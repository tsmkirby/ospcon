"""QGIS toolbar for building, tracing, and splicing outside plant connectivity."""

from __future__ import annotations

import traceback

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsUnitTypes,
    QgsWkbTypes,
    Qgis,
    QgsMessageLog,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .bridge import (
    EditSession,
    add_enclosure_point,
    add_mapped_feature,
    add_panel_feature,
    apply_edits,
    apply_inserts,
    apply_plain_edits,
    ensure_id_field,
    ensure_panel_layer,
    ensure_project,
    existing_keys,
    read_features,
    read_tables,
    scan,
    split_cable,
)
from .core.associate import assignment_edits, occupied_ports, propose
from .core.catalog import feature_id
from .core.export import export_reports
from .core.fields import enclosure_id_from_prefix, media_of, next_id, parse_count, suggest_enclosure_prefix, text_of
from .core.geometry import distance, split_linestring
from .core.link import RowInsert, plan_links
from .core.panels import panel_feature_ids, suggest_panel_id
from .core.place import (
    cable_layer_choices,
    device_layer_choices,
    features_along_line,
    idfs_serving_cable,
    name_taken,
    nearest_cable,
    nearer_end_name,
    panel_click_edits,
    panel_media_groups,
    panels_for_idfs,
    placement_edits,
    taken_cable_ids,
    taken_device_names,
)
from .core.strands import build_splice_rows
from .core.trace import format_report, trace_selection
from .dialogs import AssociateDialog, ExportDialog, PatchPanelDialog, PlacementDialog, SpliceDialog


class SketchTool(QgsMapTool):
    """Left-click adds a vertex. Right-click or Enter finishes the line."""

    def __init__(self, canvas, finished):
        super().__init__(canvas)
        self.finished = finished
        self.points = []
        self.band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.band.setColor(QColor(230, 120, 20))
        self.band.setWidth(2)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.points.append(self.toMapCoordinates(event.pos()))
            self._redraw()
        elif event.button() == Qt.RightButton:
            self._finish()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish()
        elif event.key() == Qt.Key_Escape:
            self._clear()
        else:
            super().keyPressEvent(event)

    def deactivate(self):
        self._clear()
        super().deactivate()

    def _finish(self):
        if len(self.points) < 2:
            return
        points = list(self.points)
        self._clear()
        self.finished(points)

    def _redraw(self):
        self.band.reset(QgsWkbTypes.LineGeometry)
        for point in self.points:
            self.band.addPoint(point)

    def _clear(self):
        self.points = []
        self.band.reset(QgsWkbTypes.LineGeometry)


class EnclosureMapTool(QgsMapTool):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.callback = callback
        self.setCursor(Qt.CrossCursor)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.callback(self.toMapCoordinates(event.pos()))


class OspConnectivityPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self.actions = []
        self.dock = None
        self.report = None
        self.bands = []
        self.tool = None
        self._reports = []

    def initGui(self):
        self.toolbar = self.iface.addToolBar("OSP Connectivity")
        self.toolbar.setObjectName("OspConnectivity")
        self._action("Build associations", self.build_associations)
        self._action("Associate selection", self.associate_selection)
        self._action("Trace selection", self.trace_selection)
        self._action("Add fiber enclosure", self.start_enclosure)
        self._action("Add patch panel", self.start_panel)
        self._action("Add device", self.start_device)
        self._action("Add cable", self.start_cable)
        self._action("Set snap tolerance", self.set_tolerance)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Association and trace reports appear here.")
        copy_button = QPushButton("Copy")
        export_button = QPushButton("Export")
        clear_button = QPushButton("Clear highlight")
        copy_button.clicked.connect(self._copy)
        export_button.clicked.connect(self._export)
        clear_button.clicked.connect(self.clear_highlight)
        row = QHBoxLayout()
        row.addWidget(copy_button)
        row.addWidget(export_button)
        row.addWidget(clear_button)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.addWidget(self.report)
        layout.addLayout(row)
        self.dock = QDockWidget("OSP Report", self.iface.mainWindow())
        self.dock.setObjectName("OspConnectivityReport")
        self.dock.setWidget(holder)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

    def unload(self):
        self.clear_highlight()
        canvas = self.iface.mapCanvas()
        if self.tool is not None:
            canvas.unsetMapTool(self.tool)
            self.tool = None
        for action in self.actions:
            self.toolbar.removeAction(action)
        if self.toolbar is not None:
            del self.toolbar
            self.toolbar = None
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def build_associations(self):
        self._run("Build associations", self._build)

    def associate_selection(self):
        self._run("Associate selection", self._associate)

    def trace_selection(self):
        self._run("Trace selection", self._trace)

    def start_enclosure(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("Add fiber enclosure", lambda: self._enclose(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click a fiber cable to add an enclosure.")

    def start_panel(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("Add patch panel", lambda: self._add_panel(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click an IDF to add a patch panel.")

    def start_device(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("Add device", lambda: self._add_device(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click the map to add a device.")

    def start_cable(self):
        canvas = self.iface.mapCanvas()
        self.tool = SketchTool(canvas, lambda points: self._run("Add cable", lambda: self._add_cable(points)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Left-click the cable path. Right-click or Enter to finish.")

    def set_tolerance(self):
        project = QgsProject.instance()
        value, accepted = QInputDialog.getDouble(
            self.iface.mainWindow(),
            "Snap tolerance",
            "Distance in project map units. Duct chaining and enclosure clicks use this.",
            self._tolerance(project),
            0.000001,
            1000,
            3,
        )
        if accepted:
            QgsSettings().setValue("osp_connectivity/tolerance", value)

    def clear_highlight(self):
        canvas = self.iface.mapCanvas()
        for band in self.bands:
            canvas.scene().removeItem(band)
        self.bands = []
        canvas.refresh()

    def _build(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        tables = read_tables(layers, infos)
        keys = existing_keys(features, tables)
        plan = plan_links(
            features,
            existing_strands=keys["strands"],
            existing_routes=keys["routes"],
            existing_circuits=keys["circuits"],
            existing_mounts=keys["mounts"],
            tolerance=self._tolerance(project),
            write_mount_rows="mounted_device" in store,
        )
        session = EditSession()
        skipped = apply_edits(session, layers, infos, plan.edits)
        written = apply_inserts(session, store, plan.inserts)
        errors = session.commit()
        lines = [f"{notice.level.title()}: {notice.message}" for notice in plan.notices]
        lines.append(f"Field updates: {len(plan.edits) - len(skipped)}")
        lines.append(f"Rows added: {written}")
        lines.append("Save layer edits to keep this. Undo reverses it until then.")
        for note in skipped:
            lines.append(f"Skipped: {note}")
        for error in errors:
            lines.append(f"Error: {error}")
        self._show("\n".join(lines))
        if errors:
            raise RuntimeError("\n".join(errors))

    def _associate(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        selected = self._selected(layers, features)
        proposal = propose(selected, features, self._tolerance(project))
        tables = read_tables(layers, infos)
        dialog = AssociateDialog(proposal, occupied_ports(features, tables.get("terminations") or []), self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return
        result = assignment_edits(proposal, dialog.choices())
        session = EditSession()
        try:
            skipped = apply_plain_edits(session, layers, infos, result["edits"])
            inserts = []
            if result.get("circuit"):
                inserts.append(RowInsert("circuit", result["circuit"], "circuit"))
            for route in result.get("routes") or []:
                inserts.append(RowInsert("cable_route", route, "route"))
            if result.get("mount"):
                inserts.append(RowInsert("mounted_device", result["mount"], "mount"))
            self._termination_inserts(inserts, proposal, result, store)
            written = apply_inserts(session, store, inserts)
            panel_note = self._create_panel(session, project, layers, infos, result.get("new_panel"))
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        lines = list(result.get("warnings") or [])
        if panel_note:
            lines.append(panel_note)
        lines.append(f"Field updates prepared: {len(result['edits'])}")
        lines.append(f"Rows added: {written}")
        lines.append("Save layer edits to keep this. Undo reverses it until then.")
        for note in skipped:
            lines.append(f"Skipped field: {note}")
        for error in errors:
            lines.append(f"Error: {error}")
        self._show("\n".join(lines))
        if errors:
            raise RuntimeError("\n".join(errors))

    def _trace(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        features = read_features(project, layers, infos)
        selected = self._selected(layers, features)
        tables = read_tables(layers, infos)
        result = trace_selection(selected, features, tables)
        self._reports = list(result.get("reports") or [])
        self._show(format_report(result))
        self._highlight(layers, result.get("highlights") or {})

    def _create_panel(self, session, project, layers, infos, spec) -> str:
        if not spec:
            return ""
        if spec.get("x") is None or spec.get("y") is None:
            return f"{spec['panel_id']} was not created because {spec['idf_id']} has no location."
        _layers, infos, layer = ensure_panel_layer(project, layers, infos, spec.get("media") == "fiber")
        info = next(item for item in infos if item.layer_id == layer.id())
        add_panel_feature(
            session,
            project,
            layer,
            info,
            (spec["x"], spec["y"]),
            spec["panel_id"],
            spec["idf_id"],
            spec.get("media") or "copper",
        )
        return f"Patch panel {spec['panel_id']} was added for {spec['idf_id']}."

    def _add_panel(self, canvas_point):
        project, point_xy, layers, infos, features = self._click_context(canvas_point)
        tolerance = self._tolerance(project)
        idf = self._idf_at(project, layers, features, point_xy, tolerance)
        if idf is None:
            raise RuntimeError("Click an IDF to add a patch panel.")
        groups = panel_media_groups(point_xy, features, tolerance)
        for group in groups:
            if len(group["devices"]) > 1:
                kind = "fiber" if group["media"] == "fiber" else "copper"
                raise RuntimeError(
                    f"More than one device is on the {kind} cables at this point. Use Associate selection."
                )
        existing = panel_feature_ids(features)
        template_style = not any(
            "osp_links.gpkg" in (info.source or "")
            for info in infos
            if info.role in ("patch_panel", "fdp", "splice_enc")
        )
        idf_id = feature_id(idf) or ""
        suggestions = {
            "copper": suggest_panel_id(existing, idf_id, False, template_style),
            "fiber": suggest_panel_id(existing, idf_id, True, template_style),
        }
        dialog_groups = []
        for group in groups:
            dialog_groups.append(
                {
                    "media": group["media"],
                    "cable_ids": [feature_id(cable) or "" for cable in group["cables"]],
                    "device_id": feature_id(group["device"]) if group.get("device") else "",
                    "idf_end": group["idf_end"],
                }
            )
        dialog = PatchPanelDialog(
            idf_id,
            dialog_groups,
            suggestions,
            existing,
            occupied_ports(features, read_tables(layers, infos).get("terminations") or []),
            self.iface.mainWindow(),
        )
        if dialog.exec_() != dialog.Accepted:
            return
        choices = dialog.choices()
        for choice in choices:
            if name_taken(choice["panel_id"], existing):
                raise RuntimeError(f"{choice['panel_id']} already exists.")
        layers, infos, store = ensure_project(project, layers, infos)
        for choice in choices:
            layers, infos, _panel_layer = ensure_panel_layer(
                project, layers, infos, choice.get("media") == "fiber"
            )
        features = read_features(project, layers, infos)
        idf = self._same_feature(features, idf)
        session = EditSession()
        notes = []
        warnings = []
        try:
            edits = []
            known_panels = panel_feature_ids(features)
            for choice, group in zip(choices, groups or [None]):
                if not choice.get("associate") or group is None:
                    spec = {
                        "panel_id": choice["panel_id"],
                        "idf_id": feature_id(idf),
                        "media": choice.get("media") or "copper",
                        "x": point_xy[0],
                        "y": point_xy[1],
                    }
                    notes.append(self._create_panel(session, project, layers, infos, spec))
                    continue
                cables = [item for item in (self._same_feature(features, cable) for cable in group["cables"]) if item]
                cable = self._same_feature(features, group["cable"])
                device = self._same_feature(features, group.get("device")) if group.get("device") else None
                choice["cable_layer_id"] = (cable or {}).get("layer_id")
                if device:
                    choice["device_layer_id"] = device.get("layer_id")
                plan = panel_click_edits(
                    choice,
                    cables,
                    cable,
                    device,
                    idf,
                    point_xy,
                    known_panels,
                    choice.get("media") or group["media"],
                )
                notes.append(self._create_panel(session, project, layers, infos, plan.get("new_panel")))
                edits.extend(plan["edits"])
                warnings.extend(plan["warnings"])
                self._store_circuit(session, store, plan.get("circuit"))
                if plan.get("new_panel"):
                    known_panels.append(plan["new_panel"]["panel_id"])
            skipped = apply_plain_edits(session, layers, infos, edits)
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        if errors:
            raise RuntimeError("\n".join(errors))
        lines = [note for note in notes if note]
        associated = [feature_id(cable) or "?" for group in groups for cable in group["cables"]]
        if associated:
            lines.append("Cables associated: " + ", ".join(associated))
        else:
            lines.append("No cable crossed this point.")
        lines.extend(f"Warning: {warning}" for warning in warnings)
        for note in skipped:
            lines.append(f"Skipped field: {note}")
        lines.append("Save layer edits to keep this. Undo reverses it until then.")
        self._show("\n".join(lines))
        self.iface.mapCanvas().refresh()

    def _idf_at(self, project, layers, features, point_xy, tolerance):
        point = QgsGeometry.fromPointXY(QgsPointXY(point_xy[0], point_xy[1]))
        ranked = []
        for feature in features:
            if feature.get("role") != "idf":
                continue
            layer = layers.get(feature["layer_id"])
            if layer is None:
                continue
            found = next(layer.getFeatures(QgsFeatureRequest(feature["fid"])), None)
            if found is None or not found.hasGeometry():
                continue
            geometry = QgsGeometry(found.geometry())
            if layer.crs() != project.crs():
                geometry.transform(QgsCoordinateTransform(layer.crs(), project.crs(), project))
            if geometry.isEmpty():
                continue
            contains = geometry.contains(point)
            gap = geometry.distance(point)
            if not contains and gap > tolerance:
                continue
            ranked.append((0 if contains else 1, geometry.area() if contains else gap, feature))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0][2]

    def _same_feature(self, features, feature):
        if feature is None:
            return None
        for item in features:
            if item.get("layer_id") == feature.get("layer_id") and item.get("fid") == feature.get("fid"):
                return item
        return feature

    def _enclose(self, canvas_point):
        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), project.crs(), project)
        point = transform.transform(canvas_point)
        point_xy = (point.x(), point.y())
        layers, infos = scan(project)
        features = read_features(project, layers, infos)
        tolerance = self._tolerance(project)
        hits = []
        for feature in features:
            if feature.get("role") != "cable" or media_of(feature.get("values") or {}) != "fiber":
                continue
            result = split_linestring(feature.get("coords") or [], point_xy, tolerance)
            if result["kind"] in ("through", "endpoint"):
                hits.append((feature, result))
        through = [(feature, result) for feature, result in hits if result["kind"] == "through"]
        laterals = [(feature, result) for feature, result in hits if result["kind"] == "endpoint"]
        if not through:
            raise RuntimeError("Click on a fiber cable, away from its ends, to add an enclosure.")
        if len(through) > 1:
            self.iface.messageBar().pushInfo(
                "OSP Connectivity",
                "Two through cables were split, so this enclosure has four cable ends.",
            )

        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        existing_enclosures = [
            feature_id(feature) for feature in features if feature.get("role") == "splice_enc" and feature_id(feature)
        ]
        suggested = suggest_enclosure_prefix(feature_id(through[0][0]) or "")
        strand_keys = existing_keys(features, read_tables(layers, infos))["strands"]
        reserved = []
        planned = []
        try:
            for index, (feature, result) in enumerate(through):
                known = self._ids_on_layer(features, feature["layer_id"]) + reserved
                new_id = next_id(known, feature_id(feature) or "OSP-0000")
                reserved.append(new_id)
                edits = self._ask_splice(
                    feature,
                    new_id,
                    laterals,
                    suggested if index == 0 else planned[0][3]["prefix"],
                    index == 0,
                )
                planned.append((feature, result, new_id, edits))
        except Cancelled:
            return
        enclosure = enclosure_id_from_prefix(existing_enclosures, planned[0][3]["prefix"])
        session = EditSession()
        try:
            add_enclosure_point(session, project, store["splice_enc"], point_xy, enclosure)
            for feature, result, new_id, edits in planned:
                split_cable(session, project, layers, infos, feature, result, new_id, enclosure)
                self._membership(session, store, enclosure, feature)
                self._membership_values(session, store, enclosure, feature["layer_id"], new_id)
                self._write_splices(session, store, feature, new_id, enclosure, edits, strand_keys)
            for feature, _result in laterals:
                self._membership(session, store, enclosure, feature)
                self._label_near_end(session, layers, infos, feature, point_xy, enclosure)
            errors = session.commit()
        except Cancelled:
            session.rollback()
            return
        except Exception:
            session.rollback()
            raise
        if errors:
            raise RuntimeError("\n".join(errors))
        self.iface.mapCanvas().refresh()
        self._show(
            f"Enclosure {enclosure} was added. The split segments are linked to each other and to the enclosure.\n"
            "Save layer edits to keep this. Undo reverses it until then."
        )

    def _ask_splice(self, feature, new_id, laterals, prefix, prefix_enabled):
        count = parse_count((feature.get("values") or {}).get("strand_count")) or 0
        targets = [{"id": new_id, "layer_id": feature["layer_id"]}]
        for lateral, _result in laterals:
            targets.append({"id": feature_id(lateral), "layer_id": lateral["layer_id"]})
        dialog = SpliceDialog(
            count or 12,
            feature_id(feature) or "",
            targets,
            self.iface.mainWindow(),
            prefix=prefix or "OSP",
            prefix_enabled=prefix_enabled,
        )
        if dialog.exec_() != dialog.Accepted:
            raise Cancelled()
        return dialog.result_edits()

    def _write_splices(self, session, store, feature, new_id, enclosure, edits, strand_keys) -> None:
        count = edits["strand_count"]
        rows = build_splice_rows(
            count,
            feature_id(feature),
            new_id,
            enclosure,
            edits["fusions"],
            edits["opens"],
            feature["layer_id"],
            feature["layer_id"],
        )
        inserts = []
        for number in range(1, count + 1):
            for cable_id in (feature_id(feature), new_id):
                key = (feature["layer_id"], str(cable_id).casefold(), number)
                if key in strand_keys:
                    continue
                strand_keys.add(key)
                inserts.append(
                    RowInsert(
                        "fiber_strand",
                        {
                            "strand_id": f"{cable_id}.{number}",
                            "source_layer_id": feature["layer_id"],
                            "cable_id": cable_id,
                            "strand_no": number,
                            "status": "spare",
                        },
                        "strand",
                    )
                )
        for row in rows:
            row["splice_id"] = (
                f"{enclosure}.{row['a_cable_id']}.{row['a_strand_no']}.{row.get('b_cable_id')}.{row.get('b_strand_no')}"
            )
            inserts.append(RowInsert("fiber_splice", row, "splice"))
        apply_inserts(session, store, inserts)
        self._set_strand_count(session, feature["layer_id"], [feature_id(feature), new_id], count)

    def _set_strand_count(self, session, layer_id, cable_ids, count) -> None:
        project_layers, infos = scan(QgsProject.instance())
        info = next((item for item in infos if item.layer_id == layer_id), None)
        layer = project_layers.get(layer_id)
        if info is None or layer is None or "strand_count" not in info.field_map or "id" not in info.field_map:
            return
        session.edit(layer)
        count_index = layer.fields().indexFromName(info.field_map["strand_count"])
        id_index = layer.fields().indexFromName(info.field_map["id"])
        if count_index < 0 or id_index < 0:
            return
        wanted = {str(cable_id) for cable_id in cable_ids if cable_id}
        for existing in layer.getFeatures():
            if str(existing[id_index] or "") in wanted:
                layer.changeAttributeValue(existing.id(), count_index, count)

    def _membership(self, session, store, enclosure, feature) -> None:
        self._membership_values(session, store, enclosure, feature["layer_id"], feature_id(feature))

    def _membership_values(self, session, store, enclosure, layer_id, cable_id) -> None:
        apply_inserts(
            session,
            store,
            [
                RowInsert(
                    "splice_enc_cable",
                    {"splice_enc_id": enclosure, "cable_layer_id": layer_id, "cable_id": cable_id},
                    "membership",
                )
            ],
        )

    def _label_near_end(self, session, layers, infos, feature, point, enclosure) -> None:
        coords = feature.get("coords") or []
        if len(coords) < 2:
            return
        canonical = "a_end" if distance(coords[0], point) <= distance(coords[-1], point) else "z_end"
        if (feature.get("values") or {}).get(canonical):
            return
        info = next(item for item in infos if item.layer_id == feature["layer_id"])
        layer = layers[feature["layer_id"]]
        actual = info.field_map.get(canonical)
        if not actual:
            return
        session.edit(layer)
        index = layer.fields().indexFromName(actual)
        if index >= 0:
            layer.changeAttributeValue(feature["fid"], index, enclosure)

    def _termination_inserts(self, inserts, proposal, result, store) -> None:
        circuit = result.get("circuit") or {}
        panel = circuit.get("panel_id")
        port = circuit.get("panel_port")
        if not panel or not port or not proposal.cables:
            return
        cable = proposal.cables[0]
        if proposal.media == "copper" and "copper_termination" in store:
            inserts.append(
                RowInsert(
                    "copper_termination",
                    {
                        "cable_id": feature_id(cable),
                        "pp_id": panel,
                        "panel_port": port,
                        "status": circuit.get("status") or "proposed",
                    },
                    "copper",
                )
            )
        if proposal.media == "fiber" and "fiber_termination" in store and circuit.get("strand_from"):
            end = int(circuit.get("strand_to") or circuit["strand_from"])
            for number in range(int(circuit["strand_from"]), end + 1):
                inserts.append(
                    RowInsert(
                        "fiber_termination",
                        {
                            "cable_id": feature_id(cable),
                            "strand_no": number,
                            "fdp_id": panel,
                            "panel_position": port,
                            "status": circuit.get("status") or "proposed",
                        },
                        "fiber",
                    )
                )

    def _highlight(self, layers, highlights) -> None:
        self.clear_highlight()
        canvas = self.iface.mapCanvas()
        styles = {
            "cables": (QgsWkbTypes.LineGeometry, QColor(230, 120, 20), 3),
            "devices": (QgsWkbTypes.PointGeometry, QColor(20, 90, 200), 4),
            "idfs": (QgsWkbTypes.PolygonGeometry, QColor(20, 140, 60), 2),
            "enclosures": (QgsWkbTypes.PointGeometry, QColor(180, 30, 160), 4),
        }
        bands = {}
        for key, (kind, color, width) in styles.items():
            band = QgsRubberBand(canvas, kind)
            band.setColor(color)
            band.setWidth(width)
            if kind == QgsWkbTypes.PolygonGeometry:
                fill = QColor(color)
                fill.setAlpha(50)
                band.setFillColor(fill)
            bands[key] = band
            self.bands.append(band)
        idf_points = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        idf_points.setColor(QColor(20, 140, 60))
        idf_points.setWidth(4)
        self.bands.append(idf_points)
        for key, refs in highlights.items():
            for layer_id, fid in refs:
                layer = layers.get(layer_id)
                if layer is None:
                    continue
                found = next(layer.getFeatures(QgsFeatureRequest(fid)), None)
                if found is None or not found.hasGeometry():
                    continue
                geometry = QgsGeometry(found.geometry())
                if key == "idfs" and geometry.type() == QgsWkbTypes.PointGeometry:
                    idf_points.addGeometry(geometry, layer)
                else:
                    bands[key].addGeometry(geometry, layer)
        canvas.refresh()

    def _selected(self, layers, features) -> list:
        chosen = []
        for feature in features:
            layer = layers.get(feature["layer_id"])
            if layer is not None and feature["fid"] in layer.selectedFeatureIds():
                chosen.append(feature)
        return chosen

    def _ids_on_layer(self, features, layer_id) -> list:
        return [feature_id(feature) for feature in features if feature.get("layer_id") == layer_id and feature_id(feature)]

    def _add_device(self, canvas_point):
        project, point_xy, layers, infos, features = self._click_context(canvas_point)
        choices = device_layer_choices(infos)
        if not choices:
            raise RuntimeError("This project has no AP, CCTV, POS, or other device layer to add to.")
        tolerance = self._tolerance(project)
        cables = [feature for feature in features if feature.get("role") == "cable"]
        cable = nearest_cable(point_xy, cables, tolerance)
        idfs = idfs_serving_cable(cable, features) if cable else []
        context = self._placement_context(cable, idfs, features, point_xy, ask_media=False)
        dialog = PlacementDialog(
            "Add device",
            "Device type",
            choices,
            "Device name",
            "",
            taken_device_names(features),
            context,
            occupied_ports(features, read_tables(layers, infos).get("terminations") or []),
            self.iface.mainWindow(),
        )
        if dialog.exec_() != dialog.Accepted:
            return
        choice = dialog.choices()
        choice["device_layer_id"] = choice.get("layer_id")
        if cable:
            choice["cable_layer_id"] = cable.get("layer_id")
        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        cable = self._same_feature(features, cable) if cable else None
        idf = self._chosen_idf(features, idfs, choice.get("idf_id"))
        target = layers.get(choice["layer_id"])
        if target is None:
            raise RuntimeError("The device layer is no longer in the project.")
        ensure_id_field(target, choice.get("role") or "pos")
        layers, infos = scan(project)
        target = layers.get(choice["layer_id"])
        info = next(item for item in infos if item.layer_id == choice["layer_id"])
        plan = placement_edits(
            choice,
            cable,
            None,
            idf,
            new_device_id=choice["name"],
            panel_feature_ids=panel_feature_ids(features),
            media=choice.get("media") or context.get("media") or "copper",
        )
        session = EditSession()
        try:
            add_mapped_feature(session, project, target, info, point_xy, plan["device_values"], "point")
            skipped = apply_plain_edits(session, layers, infos, plan["edits"])
            panel_note = self._create_panel(session, project, layers, infos, plan.get("new_panel"))
            self._store_circuit(session, store, plan.get("circuit"))
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        self._placement_report(choice["name"], plan, panel_note, skipped, errors)

    def _add_cable(self, canvas_points):
        project = QgsProject.instance()
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(canvas_crs, project.crs(), project)
        line = []
        for point in canvas_points:
            moved = transform.transform(point)
            line.append((moved.x(), moved.y()))
        layers, infos = scan(project)
        features = read_features(project, layers, infos)
        kinds = cable_layer_choices(infos)
        if not kinds:
            raise RuntimeError("This project has no cable layer to add to.")
        tolerance = self._tolerance(project)
        devices = features_along_line(line, features, {"ap", "cctv", "data_jack", "pos"}, tolerance)
        panels = features_along_line(line, features, {"patch_panel", "fdp"}, tolerance)
        idfs = self._idfs_along_line(project, layers, features, line, tolerance)
        for panel in panels:
            parent = text_of((panel.get("values") or {}).get("idf_ref"))
            if parent and all(feature_id(item) != parent for item in idfs):
                found = next(
                    (feature for feature in features if feature.get("role") == "idf" and feature_id(feature) == parent),
                    None,
                )
                if found:
                    idfs.append(found)
        device = devices[0] if devices else None
        if len(devices) > 1:
            raise RuntimeError("More than one device is on this cable. Place it with one device, or use Associate selection.")
        associate = bool(device and (idfs or panels))
        media = media_of((device.get("values") or {}) if device else {})
        if media == "unknown":
            media = "copper"
        if any(media_of(item.get("values") or {}) == "fiber" for item in panels):
            media = "fiber"
        context = {
            "associate": associate,
            "ask_media": True,
            "media": media,
            "idfs": idfs,
            "panels_by_idf": panels_for_idfs(idfs, features) if idfs else {},
            "panel_id": feature_id(panels[0]) if panels else "",
            "device_end": nearer_end_name(line, (device.get("coords") or [None])[0]) if device and device.get("coords") else "a_end",
            "summary": self._cable_summary(device, idfs, panels),
        }
        existing_ids = taken_cable_ids(features)
        sample = next((item for item in existing_ids if item), "OSP-CB-0000")
        dialog = PlacementDialog(
            "Add cable",
            "Cable layer",
            kinds,
            "Cable id",
            next_id(existing_ids, sample),
            existing_ids,
            context,
            occupied_ports(features, read_tables(layers, infos).get("terminations") or []),
            self.iface.mainWindow(),
        )
        if dialog.exec_() != dialog.Accepted:
            return
        choice = dialog.choices()
        choice["cable_layer_id"] = choice.get("layer_id")
        if device:
            choice["device_layer_id"] = device.get("layer_id")
        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        device = self._same_feature(features, device) if device else None
        idf = self._chosen_idf(features, idfs, choice.get("idf_id"))
        target = layers.get(choice["layer_id"])
        info = next(item for item in infos if item.layer_id == choice["layer_id"])
        plan = placement_edits(
            choice,
            None,
            device,
            idf,
            new_cable_id=choice["name"],
            panel_feature_ids=panel_feature_ids(features),
            media=choice.get("media") or "copper",
        )
        session = EditSession()
        try:
            add_mapped_feature(session, project, target, info, line, plan["cable_values"], "line")
            skipped = apply_plain_edits(session, layers, infos, plan["edits"])
            panel_note = self._create_panel(session, project, layers, infos, plan.get("new_panel"))
            self._store_circuit(session, store, plan.get("circuit"))
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        self._placement_report(choice["name"], plan, panel_note, skipped, errors)

    def _placement_context(self, cable, idfs, features, point, ask_media):
        if cable is None:
            return {"associate": False, "ask_media": ask_media, "media": "copper"}
        media = media_of(cable.get("values") or {})
        if media == "unknown":
            media = "copper"
        return {
            "associate": True,
            "ask_media": ask_media,
            "media": media,
            "idfs": idfs,
            "panels_by_idf": panels_for_idfs(idfs, features) if idfs else {},
            "device_end": nearer_end_name(cable.get("coords") or [], point),
            "summary": f"Cable: {feature_id(cable) or '?'}",
        }

    def _cable_summary(self, device, idfs, panels) -> str:
        parts = []
        if device:
            parts.append(f"Device: {feature_id(device) or '?'}")
        if idfs:
            parts.append("IDF: " + ", ".join(feature_id(item) or "?" for item in idfs))
        if panels:
            parts.append("Panel: " + ", ".join(feature_id(item) or "?" for item in panels))
        return "\n".join(parts)

    def _chosen_idf(self, features, idfs, idf_id):
        wanted = text_of(idf_id)
        refreshed = [item for item in (self._same_feature(features, item) for item in idfs) if item]
        if wanted:
            match = next((item for item in refreshed if feature_id(item) == wanted), None)
            if match:
                return match
            return next((item for item in features if item.get("role") == "idf" and feature_id(item) == wanted), None)
        return refreshed[0] if refreshed else None

    def _idfs_along_line(self, project, layers, features, line, tolerance):
        geometry = QgsGeometry.fromPolylineXY([QgsPointXY(point[0], point[1]) for point in line])
        found = []
        for feature in features:
            if feature.get("role") != "idf":
                continue
            layer = layers.get(feature["layer_id"])
            if layer is None:
                continue
            existing = next(layer.getFeatures(QgsFeatureRequest(feature["fid"])), None)
            if existing is None or not existing.hasGeometry():
                continue
            candidate = QgsGeometry(existing.geometry())
            if layer.crs() != project.crs():
                candidate.transform(QgsCoordinateTransform(layer.crs(), project.crs(), project))
            if candidate.intersects(geometry) or candidate.distance(geometry) <= tolerance:
                found.append(feature)
        return found

    def _click_context(self, canvas_point):
        project = QgsProject.instance()
        transform = QgsCoordinateTransform(self.iface.mapCanvas().mapSettings().destinationCrs(), project.crs(), project)
        point = transform.transform(canvas_point)
        layers, infos = scan(project)
        return project, (point.x(), point.y()), layers, infos, read_features(project, layers, infos)

    def _store_circuit(self, session, store, circuit) -> None:
        if not circuit or not store:
            return
        apply_inserts(session, store, [RowInsert("circuit", circuit, "circuit")])

    def _placement_report(self, name, plan, panel_note, skipped, errors) -> None:
        if errors:
            raise RuntimeError("\n".join(errors))
        lines = [f"{name} was added."]
        if plan.get("warnings"):
            lines.extend(f"Warning: {warning}" for warning in plan["warnings"])
        if panel_note:
            lines.append(panel_note)
        for note in skipped:
            lines.append(f"Skipped field: {note}")
        lines.append("Save layer edits to keep this. Undo reverses it until then.")
        self._show("\n".join(lines))
        self.iface.mapCanvas().refresh()

    def _tolerance(self, project) -> float:
        settings = QgsSettings()
        stored = settings.value("osp_connectivity/tolerance", None)
        if stored not in (None, ""):
            return float(stored)
        if project.crs().mapUnits() == QgsUnitTypes.DistanceDegrees:
            return 0.00002
        return 1.0

    def _show(self, text: str) -> None:
        self.report.setPlainText(text)
        self.dock.show()
        self.dock.raise_()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.report.toPlainText())

    def _export(self) -> None:
        if not self._reports:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Export report",
                "Trace a selection first. The spreadsheet is built from that report.",
            )
            return
        dialog = ExportDialog(self._reports, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return
        choice = dialog.choices()
        if choice["mode"] == "append":
            path, _filter = QFileDialog.getOpenFileName(
                self.iface.mainWindow(),
                "Add to spreadsheet",
                "",
                "Spreadsheets (*.csv *.xlsx)",
            )
            append = True
        else:
            suffix = "xlsx" if choice["format"] == "xlsx" else "csv"
            label = "Excel (*.xlsx)" if suffix == "xlsx" else "CSV (*.csv)"
            path, _filter = QFileDialog.getSaveFileName(
                self.iface.mainWindow(),
                "Export OSP report",
                "",
                label,
            )
            if path and not path.lower().endswith("." + suffix):
                path = f"{path}.{suffix}"
            append = False
        if not path:
            return
        written = export_reports(path, choice["reports"], choice["fields"], append)
        verb = "Added" if append else "Wrote"
        self.iface.messageBar().pushInfo("OSP Connectivity", f"{verb} {written} row(s) in {path}.")

    def _run(self, title, function) -> None:
        try:
            function()
        except Exception as exc:
            QgsMessageLog.logMessage(traceback.format_exc(), "OSP Connectivity", Qgis.Critical)
            QMessageBox.critical(self.iface.mainWindow(), title, str(exc))

    def _action(self, text, slot) -> None:
        action = QAction(text, self.iface.mainWindow())
        action.triggered.connect(slot)
        self.toolbar.addAction(action)
        self.actions.append(action)


class Cancelled(Exception):
    """The enclosure dialog was closed without saving."""
