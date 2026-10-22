"""QGIS toolbar for building, tracing, and splicing outside plant connectivity."""

from __future__ import annotations

import traceback
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QUrl, QSize
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
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
    QgsSnappingConfig,
    QgsUnitTypes,
    QgsWkbTypes,
    Qgis,
    QgsMessageLog,
)
from qgis.gui import QgsMapCanvasTracer, QgsMapTool, QgsMapToolCapture, QgsRubberBand

from .bridge import (
    EditSession,
    add_enclosure_point,
    add_mapped_feature,
    unset_fields,
    add_panel_feature,
    add_table_row,
    ensure_switch_tables,
    apply_edits,
    apply_inserts,
    apply_plain_edits,
    delete_features,
    update_feature_fields,
    ensure_id_field,
    ensure_panel_layer,
    ensure_project,
    ensure_splice_fields,
    existing_keys,
    read_features,
    read_tables,
    scan,
    split_cable,
)
from .core.associate import assignment_edits, device_association, modification_edits, occupied_ports, propose
from .core.cards import write_circuit_cards
from .core.catalog import feature_id
from .core.enclosures import enclosure_association, enclosure_edits, enclosure_end_links
from .core.splice_editor import editor_model, splice_changes
from .core.export import export_reports
from .core.fields import enclosure_id_from_prefix, mapped_values, media_of, next_id, parse_count, parse_range, suggest_enclosure_prefix, text_of
from .core.geometry import distance, split_linestring
from .core.link import RowInsert, plan_links
from .core.panels import panel_feature_ids, panel_ports
from .core.place import (
    cable_layer_choices,
    device_layer_choices,
    features_along_line,
    idfs_serving_cable,
    name_taken,
    adopt_typed_id,
    nearest_cable,
    nearest_feature,
    nearer_end_name,
    panel_click_edits,
    panel_media_groups,
    panels_for_idfs,
    placement_edits,
    taken_cable_ids,
    taken_device_names,
)
from .core.strands import build_splice_rows
from .core.switches import choice_label, expand_switch_ports, panels_on_idf, switch_inventory, switches_for_idf
from .core.trace import format_report, trace_selection
from .core.naming import local_asset_id, scoped_asset_id
from .dialogs import (
    AssociateDialog,
    BlankAttributeDialog,
    CircuitCardDialog,
    ExportDialog,
    ModifyDialog,
    ChooseAssetDialog,
    PanelViewDialog,
    PatchDialog,
    PatchPanelDialog,
    PlacementDialog,
    PortCountDialog,
    SpliceDialog,
    SwitchDialog,
)
from .mapimage import render_circuit_jpegs
from .splice_dialog import SpliceEditorDialog


class SketchTool(QgsMapToolCapture):
    """Draw a cable with the same snapping and tracing as the line tool."""

    def __init__(self, canvas, cad_dock, finished):
        super().__init__(canvas, cad_dock, QgsMapToolCapture.CaptureLine)
        self.finished = finished
        self._saved_snap = None
        self._tracing_action = None
        self.setAutoSnapEnabled(True)

    def activate(self):
        super().activate()
        self._arm_snapping()

    def deactivate(self):
        self._restore_snapping()
        if self.isCapturing():
            self.stopCapturing()
        super().deactivate()

    def cadCanvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.addVertex(event.mapPoint(), event.mapPointMatch())
        elif event.button() == Qt.RightButton:
            self._finish()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish()
        elif event.key() == Qt.Key_Escape:
            self.stopCapturing()
        elif event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.undo()
        else:
            super().keyPressEvent(event)

    def _finish(self):
        points = [QgsPointXY(point) for point in self.points()]
        source_crs = _capture_crs(self)
        self.stopCapturing()
        if len(points) >= 2:
            self.finished(points, source_crs)

    def _arm_snapping(self):
        project = QgsProject.instance()
        config = project.snappingConfig()
        self._saved_snap = {
            "enabled": config.enabled(),
            "mode": config.mode(),
            "type": config.typeFlag(),
            "tolerance": config.tolerance(),
            "units": config.units(),
        }
        config.setEnabled(True)
        config.setMode(QgsSnappingConfig.AllLayers)
        config.setTypeFlag(QgsSnappingConfig.VertexFlag | QgsSnappingConfig.SegmentFlag)
        if not self._saved_snap["enabled"]:
            config.setUnits(Qgis.MapToolUnit.Pixels)
            config.setTolerance(12)
        project.setSnappingConfig(config)
        tracer = QgsMapCanvasTracer.tracerForCanvas(self.canvas())
        action = tracer.actionEnableTracing() if tracer is not None else None
        if action is not None and not action.isChecked():
            action.trigger()
            self._tracing_action = action

    def _restore_snapping(self):
        if self._tracing_action is not None and self._tracing_action.isChecked():
            self._tracing_action.trigger()
        self._tracing_action = None
        saved = self._saved_snap
        self._saved_snap = None
        if not saved:
            return
        config = QgsProject.instance().snappingConfig()
        config.setEnabled(saved["enabled"])
        config.setMode(saved["mode"])
        config.setTypeFlag(saved["type"])
        config.setTolerance(saved["tolerance"])
        config.setUnits(saved["units"])
        QgsProject.instance().setSnappingConfig(config)


def _capture_crs(tool):
    """CRS of the vertices from a capture tool.

    QgsMapToolCapture keeps the sketch in the current layer's CRS. A click on
    the map is only in the project CRS when that layer uses the project CRS.
    """
    layer = tool.layer()
    if layer is not None and layer.crs().isValid():
        return layer.crs()
    return tool.canvas().mapSettings().destinationCrs()


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
        self.toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.toolbar.setIconSize(QSize(24, 24))
        self._action("Build associations", self.build_associations)
        self._action("Associate selection", self.associate_selection)
        self._action("Modify associations", self.modify_associations)
        self._action("Splice Editor", self.open_splice_editor)
        self._action("Trace selection", self.trace_selection)
        self._action("Add fiber enclosure", self.start_enclosure)
        self._action("Add patch panel", self.start_panel)
        self._action("View patch panel", self.start_panel_view)
        self._action("View switch", self.start_switch_view)
        self._action("Add switch", self.start_switch)
        self._action("Patching", self.patching)
        self._action("Add device", self.start_device)
        self._action("Add cable", self.start_cable)
        self._action("Set snap tolerance", self.set_tolerance)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("Association and trace reports appear here.")
        copy_button = QPushButton("Copy")
        export_button = QPushButton("Export")
        card_button = QPushButton("Circuit card")
        clear_button = QPushButton("Clear highlight")
        copy_button.clicked.connect(self._copy)
        export_button.clicked.connect(self._export)
        card_button.clicked.connect(self._circuit_card)
        clear_button.clicked.connect(self.clear_highlight)
        row = QHBoxLayout()
        row.addWidget(copy_button)
        row.addWidget(export_button)
        row.addWidget(card_button)
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
            self.iface.removePluginMenu("OSP Connectivity", action)
            self.toolbar.removeAction(action)
            action.deleteLater()
        self.actions.clear()
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

    def modify_associations(self):
        self._run("Modify associations", self._modify)

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

    def start_switch(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("Add switch", lambda: self._add_switch(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click an IDF to add a switch.")

    def patching(self):
        self._run("Patching", self._patch)

    def start_panel_view(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("View patch panel", lambda: self._view_panel(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click an IDF to view one of its patch panels.")

    def start_switch_view(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("View switch", lambda: self._view_switch(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click an IDF to view one of its switches.")

    def start_device(self):
        canvas = self.iface.mapCanvas()
        self.tool = EnclosureMapTool(canvas, lambda point: self._run("Add device", lambda: self._add_device(point)))
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo("OSP Connectivity", "Click the map to add a device.")

    def start_cable(self):
        canvas = self.iface.mapCanvas()
        self.tool = SketchTool(
            canvas,
            self.iface.cadDockWidget(),
            lambda points, source_crs: self._run("Add cable", lambda: self._add_cable(points, source_crs)),
        )
        canvas.setMapTool(self.tool)
        self.iface.messageBar().pushInfo(
            "OSP Connectivity",
            "Left-click the cable path. It snaps to existing geometry, including duct banks. Right-click or Enter to finish.",
        )

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
        try:
            skipped = apply_edits(session, layers, infos, plan.edits)
            written = apply_inserts(session, store, plan.inserts)
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
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
            self._handhole_inserts(inserts, result.get("handholes") or [], tables)
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
        result = trace_selection(selected, features, tables, self._tolerance(project))
        self._reports = list(result.get("reports") or [])
        self._show(format_report(result))
        self._highlight(layers, result.get("highlights") or {})

    def _create_panel(self, session, project, layers, infos, spec) -> str:
        if not spec:
            return ""
        spec['panel_id'] = scoped_asset_id(spec['panel_id'], spec['idf_id'])
        label = local_asset_id(spec['panel_id'], spec['idf_id'])
        if spec.get("x") is None or spec.get("y") is None:
            return f"{label} was not created because {spec['idf_id']} has no location."
        count = spec.get("port_count")
        if not count:
            dialog = PortCountDialog(f"{spec['idf_id']} {label}", self.iface.mainWindow())
            if dialog.exec_() != dialog.Accepted:
                raise Cancelled()
            count = dialog.port_count()
            spec["port_count"] = count
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
            int(count),
        )
        return f"Patch panel {label} was added for {spec['idf_id']} with {int(count)} ports."

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
        idf_id = feature_id(idf) or ""
        local_panels = [local_asset_id(feature_id(item), idf_id) for item in features
                        if item.get('role') in ('patch_panel', 'fdp')
                        and text_of((item.get('values') or {}).get('idf_ref')) == idf_id]
        existing.extend(scoped_asset_id(name, idf_id) for name in local_panels)
        suggestions = {'copper': next_id(local_panels, 'PP-00'), 'fiber': next_id(local_panels, 'FD-00')}
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
                raise RuntimeError(f"{local_asset_id(choice['panel_id'], idf_id)} already exists on {idf_id}.")
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
        targets = [{"id": new_id, "layer_id": feature["layer_id"], "split_segment": True}]
        for lateral, _result in laterals:
            targets.append({"id": feature_id(lateral), "layer_id": lateral["layer_id"],
                            "capacity": parse_count((lateral.get("values") or {}).get("strand_count")) or 0})
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
            "handholes": (QgsWkbTypes.PointGeometry, QColor(140, 90, 20), 4),
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
            if key not in styles:
                continue
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
        structure = nearest_feature(point_xy, features, {"structure"}, tolerance)
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
        structure = self._same_feature(features, structure) if structure else None
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
            structure=structure,
        )
        attributes = self._remaining_attributes(target, info, plan["device_values"])
        if attributes is None:
            return
        adopt_typed_id(plan, info.field_map, attributes, "device")
        session = EditSession()
        try:
            add_mapped_feature(session, project, target, info, point_xy, plan["device_values"], "point", attributes)
            skipped = apply_plain_edits(session, layers, infos, plan["edits"])
            panel_note = self._create_panel(session, project, layers, infos, plan.get("new_panel"))
            self._store_circuit(session, store, plan.get("circuit"))
            self._store_mount(session, store, plan.get("mount"))
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        self._activate_for_undo(target)
        self._placement_report(choice["name"], plan, panel_note, skipped, errors)

    def _add_cable(self, captured_points, source_crs):
        project = QgsProject.instance()
        transform = QgsCoordinateTransform(source_crs, project.crs(), project)
        line = []
        for point in captured_points:
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
        links = []
        if (choice.get("media") or "copper") == "fiber":
            links = enclosure_end_links(line, features, tolerance, choice["name"], choice["layer_id"])
            for link in links:
                if not plan["cable_values"].get(link["end"]):
                    plan["cable_values"][link["end"]] = link["enclosure_id"]
        attributes = self._remaining_attributes(target, info, plan["cable_values"])
        if attributes is None:
            return
        adopt_typed_id(plan, info.field_map, attributes, "cable")
        for link in links:
            link["membership"]["cable_id"] = plan["cable_values"].get("id") or choice["name"]
        session = EditSession()
        try:
            add_mapped_feature(session, project, target, info, line, plan["cable_values"], "line", attributes)
            skipped = apply_plain_edits(session, layers, infos, plan["edits"])
            panel_note = self._create_panel(session, project, layers, infos, plan.get("new_panel"))
            self._store_circuit(session, store, plan.get("circuit"))
            if links:
                apply_inserts(
                    session,
                    store,
                    [RowInsert("splice_enc_cable", link["membership"], "membership") for link in links],
                )
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        self._activate_for_undo(target)
        names = ", ".join(link["enclosure_id"] for link in links)
        extra = [f"Enclosure: {names}"] if names else None
        self._placement_report(choice["name"], plan, panel_note, skipped, errors, extra)

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

    def _activate_for_undo(self, layer) -> None:
        """Point Edit → Undo at the layer that just received the edit."""
        if layer is not None:
            self.iface.setActiveLayer(layer)

    def _remaining_attributes(self, layer, info, values):
        """Second dialog: the id already entered, and every attribute an association did not fill."""
        fields = unset_fields(layer, info, values)
        if not fields:
            return {}
        dialog = BlankAttributeDialog(
            layer.name(),
            fields,
            self.iface.mainWindow(),
            mapped_values(getattr(info, "field_map", {}) or {}, values),
        )
        if dialog.exec_() != dialog.Accepted:
            return None
        return dialog.values()

    def _store_mount(self, session, store, mount) -> None:
        if not mount or not store:
            return
        apply_inserts(session, store, [RowInsert("mounted_device", mount, "mount")])

    def _store_circuit(self, session, store, circuit) -> None:
        if not circuit or not store:
            return
        apply_inserts(session, store, [RowInsert("circuit", circuit, "circuit")])

    def _placement_report(self, name, plan, panel_note, skipped, errors, extra=None) -> None:
        if errors:
            raise RuntimeError("\n".join(errors))
        lines = [f"{name} was added."]
        if extra:
            lines.extend(extra)
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
        if choice["mode"] in ("append", "update"):
            path, _filter = QFileDialog.getOpenFileName(
                self.iface.mainWindow(),
                "Update link tracker" if choice['mode'] == 'update' else "Add to spreadsheet",
                "",
                "Spreadsheets (*.csv *.xlsx)",
            )
            append = choice['mode'] == 'append'
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
        written = export_reports(path, choice["reports"], choice["fields"], append, update=choice['mode'] == 'update')
        verb = "Updated" if choice['mode'] == 'update' else "Added" if append else "Wrote"
        self.iface.messageBar().pushInfo("OSP Connectivity", f"{verb} {written} row(s) in {path}.")

    def _circuit_card(self) -> None:
        if not self._reports:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Circuit card",
                "Trace a selection first. The circuit card is built from that trace.",
            )
            return
        dialog = CircuitCardDialog(self._reports, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Save circuit card",
            "OSP circuit card.pdf",
            "PDF (*.pdf)",
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path = f"{path}.pdf"
        reports = dialog.choices()["reports"]
        images = self._circuit_images(reports)
        pages = write_circuit_cards(path, reports, images)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.iface.messageBar().pushInfo("OSP Connectivity", f"Wrote {pages} circuit card page(s) in {path}.")

    def _circuit_images(self, reports):
        try:
            project = QgsProject.instance()
            _layers, infos = scan(project)
            osp_ids = {info.layer_id for info in infos if info.role}
            return render_circuit_jpegs(project, self.iface.mapCanvas(), reports, osp_ids)
        except Exception:
            QgsMessageLog.logMessage(traceback.format_exc(), "OSP Connectivity", Qgis.Warning)
            self.iface.messageBar().pushWarning("OSP Connectivity", "The map image was left off the circuit card.")
            return None

    def _modify(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        layers, infos, store = ensure_project(project, layers, infos)
        features = read_features(project, layers, infos)
        selected = [feature for feature in self._selected(layers, features) if feature.get("role") in ("ap", "cctv", "data_jack", "pos")]
        enclosures = [feature for feature in self._selected(layers, features) if feature.get("role") == "splice_enc"]
        if len(enclosures) == 1 and not selected:
            self._modify_enclosure(project, layers, infos, store, features, enclosures[0])
            return
        if len(selected) != 1:
            raise RuntimeError("Select one device, or one fiber enclosure.")
        tables = read_tables(layers, infos)
        association = device_association(selected[0], features, tables, self._tolerance(project))
        if not association["ready"]:
            raise RuntimeError("\n".join(association["warnings"]) or "That device has no associations to change.")
        current_panel = (association.get("panel") or "").casefold()
        current_port = (association.get("port") or "").casefold()
        occupied = [
            pair
            for pair in occupied_ports(features, tables.get("terminations") or [])
            if not (pair[0].casefold() == current_panel and pair[1].casefold() == current_port)
        ]
        dialog = ModifyDialog(association, occupied, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return
        result = modification_edits(association, dialog.choices(), tables.get("handholes") or [])
        session = EditSession()
        try:
            delete_features(session, layers, result.get("delete") or [])
            skipped = apply_plain_edits(session, layers, infos, result["edits"])
            inserts = []
            self._handhole_inserts(inserts, result.get("handholes") or [], {"handholes": []})
            apply_inserts(session, store, inserts)
            update_feature_fields(session, layers, result.get("circuit_updates") or [])
            panel_note = self._create_panel(session, project, layers, infos, result.get("new_panel"))
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        if errors:
            raise RuntimeError("\n".join(errors))
        lines = [f"{feature_id(selected[0])} associations were updated."]
        if panel_note:
            lines.append(panel_note)
        handholes = [row.get("handhole_id") for row in result.get("handholes") or [] if row.get("handhole_id")]
        if handholes:
            lines.append("Handholes: " + " > ".join(dict.fromkeys(handholes)))
        for note in skipped:
            lines.append(f"Skipped field: {note}")
        lines.append("Save layer edits to keep this. Undo reverses it until then.")
        self._show("\n".join(lines))
        self.iface.mapCanvas().refresh()

    def open_splice_editor(self):
        self._run("Splice Editor", self._edit_selected_enclosure)

    def _edit_selected_enclosure(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        features = read_features(project, layers, infos)
        selected = [feature for feature in self._selected(layers, features) if feature.get("role") == "splice_enc"]
        if len(selected) != 1:
            raise RuntimeError("Select one fiber enclosure, then open Splice Editor.")
        layers, infos, store = ensure_project(project, layers, infos)
        self._modify_enclosure(project, layers, infos, store, features, selected[0])

    def _modify_enclosure(self, project, layers, infos, store, features, enclosure) -> None:
        tables = read_tables(layers, infos)
        model = editor_model(enclosure, features, tables, self._tolerance(project))
        def saved_route():
            current_layers, current_infos = scan(project)
            current_features = read_features(project, current_layers, current_infos)
            result = trace_selection([enclosure], current_features, read_tables(current_layers, current_infos),
                                     self._tolerance(project))
            self._reports = list(result.get('reports') or [])
            self._show(format_report(result))
            self._highlight(current_layers, result.get('highlights') or {})
            return format_report(result)

        def print_saved_route():
            saved_route()
            self._circuit_card()

        def show_saved_route():
            try:
                return saved_route()
            except Exception as exc:
                QMessageBox.warning(self.iface.mainWindow(), 'Show saved route', str(exc))
                return str(exc)

        dialog = SpliceEditorDialog(model, self.iface.mainWindow(),
                                   show_saved_route=show_saved_route,
                                   print_saved_route=lambda: self._run('Print saved route PDF', print_saved_route))
        if dialog.exec_() != dialog.Accepted:
            return
        edited_model = dialog.model
        latest = read_tables(layers, infos)
        current_rows = [row for row in latest.get("splices", [])
                        if str(row.get("splice_enc_id") or "").casefold() == (feature_id(enclosure) or "").casefold()]
        if current_rows != model["original"]:
            raise RuntimeError("The enclosure splices changed while this editor was open. Reopen the editor to use the latest connections.")
        tables = latest
        result = splice_changes(edited_model, dialog.choices())
        real_cables = [cable for cable in edited_model["cables"] if not cable.get("missing")]
        membership = enclosure_edits({"enclosure": enclosure, "cables": real_cables}, None, tables, self._tolerance(project))
        if "fiber_splice" not in store or "splice_enc_cable" not in store:
            raise RuntimeError("This project needs splice and enclosure membership tables before saving.")
        session = EditSession()
        try:
            # All validation completes before replacing any of the original splice rows.
            session.edit(store["fiber_splice"])
            ensure_splice_fields(store["fiber_splice"])
            delete_features(session, layers, result["delete"])
            skipped = apply_plain_edits(session, layers, infos, membership["edits"])
            edits = []
            for before, cable in zip(model["cables"], edited_model["cables"]):
                if cable.get("capacity") != before.get("capacity") and not cable.get("missing"):
                    edits.append({"layer_id": cable["layer_id"], "fid": cable["fid"],
                                  "canonical": "strand_count", "value": cable["capacity"]})
            skipped.extend(apply_plain_edits(session, layers, infos, edits))
            inserts = [RowInsert("fiber_splice", row, "splice") for row in result["splices"]]
            inserts.extend(RowInsert("splice_enc_cable", row, "membership") for row in membership["memberships"])
            # Use each cable's own capacity, not the source cable's fiber count.
            known = {(str(row.get("cable_id") or "").casefold(), int(row.get("strand_no") or 0),
                      row.get("source_layer_id") or "") for row in tables.get("strands", [])}
            for cable in real_cables:
                for number in range(1, int(cable.get("capacity") or 0) + 1):
                    cable_id = feature_id(cable)
                    key = (cable_id.casefold(), number, cable.get("layer_id") or "")
                    if key in known or (key[0], number, "") in known:
                        continue
                    known.add(key)
                    inserts.append(RowInsert("fiber_strand", {"strand_id": f"{cable_id}.{number}",
                        "source_layer_id": cable.get("layer_id") or "", "cable_id": cable_id,
                        "strand_no": number, "status": "spare"}, "strand"))
            apply_inserts(session, store, inserts)
            session.commit()
        except Exception:
            session.rollback()
            raise
        lines = [f"{feature_id(enclosure)} splice table updated: {len(result['splices'])} added, {len(result['delete'])} removed."]
        lines.extend(f"Skipped field: {note}" for note in skipped)
        lines.append("Save layer edits to keep this. Each affected layer has an OSP Connectivity undo step.")
        self._show("\n".join(lines))
        self._reports = []
        self.iface.mapCanvas().refresh()

    def _handhole_inserts(self, inserts, rows, tables) -> None:
        existing = set()
        for row in tables.get("handholes") or []:
            cable_id = text_of(row.get("cable_id"))
            handhole_id = text_of(row.get("handhole_id"))
            if cable_id and handhole_id:
                existing.add((cable_id.casefold(), handhole_id.casefold()))
        for row in rows:
            cable_id = text_of(row.get("cable_id"))
            handhole_id = text_of(row.get("handhole_id"))
            key = ((cable_id or "").casefold(), (handhole_id or "").casefold())
            if key in existing:
                continue
            existing.add(key)
            inserts.append(RowInsert("cable_handhole", row, "cable_handhole"))

    def _add_switch(self, canvas_point):
        project, point_xy, layers, infos, features = self._click_context(canvas_point)
        idf = self._idf_at(project, layers, features, point_xy, self._tolerance(project))
        if idf is None:
            raise RuntimeError("Click an IDF to add a switch.")
        layers, infos, store = ensure_switch_tables(project, layers, infos)
        tables = read_tables(layers, infos)
        existing = [text_of(row.get("sw_id")) for row in tables.get("switches") or [] if text_of(row.get("sw_id"))]
        idf_id = feature_id(idf) or ''
        existing.extend(scoped_asset_id(row.get('sw_id'), idf_id) for row in switches_for_idf(idf_id, tables.get('switches') or []))
        local_switches = [local_asset_id(value, idf_id) for value in existing if value.startswith(idf_id + '-')]
        dialog = SwitchDialog(next_id(local_switches, 'SW-00'), existing, self.iface.mainWindow(), idf_id=idf_id)
        if dialog.exec_() != dialog.Accepted:
            return
        choice = dialog.choices()
        ports = expand_switch_ports(choice["sw_port_count"], choice["first_port"])
        session = EditSession()
        try:
            add_table_row(
                session,
                store["switch"],
                {
                    "sw_id": choice["sw_id"],
                    "sw_name": choice["sw_name"],
                    "sw_port_count": choice["sw_port_count"],
                    "make": choice["make"],
                    "model": choice["model"],
                    "rack_number": choice["rack_number"],
                    "ru_number": choice["ru_number"],
                    "mdf_idf_id": feature_id(idf),
                    "status": "proposed",
                },
            )
            for port in ports:
                add_table_row(
                    session,
                    store["switch_port"],
                    {
                        "switch_port_id": port["switch_port_id"],
                        "switch_port_name": port["switch_port_name"],
                        "sw_id": choice["sw_id"],
                        "status": "proposed",
                    },
                )
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        if errors:
            raise RuntimeError("\n".join(errors))
        self._show(
            f"{local_asset_id(choice['sw_id'], feature_id(idf))} was added on {feature_id(idf)} with {len(ports)} ports "
            f"({ports[0]['switch_port_id']} through {ports[-1]['switch_port_id']}).\n"
            "Save layer edits to keep this. Undo reverses it until then."
        )
        self.iface.mapCanvas().refresh()

    def _patch(self):
        project = QgsProject.instance()
        layers, infos = scan(project)
        layers, infos, store = ensure_switch_tables(project, layers, infos)
        features = read_features(project, layers, infos)
        tables = read_tables(layers, infos)
        idfs = [feature for feature in features if feature.get("role") == "idf" and feature_id(feature)]
        if not idfs:
            raise RuntimeError("This project has no IDF to patch.")
        dialog = PatchDialog(idfs, features, tables, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return
        rows = dialog.patches()
        if not rows:
            return
        session = EditSession()
        try:
            for row in rows:
                add_table_row(session, store["cross_connect"], row)
            errors = session.commit()
        except Exception:
            session.rollback()
            raise
        if errors:
            raise RuntimeError("\n".join(errors))
        self._show(
            f"Added {len(rows)} patch(es).\nSave layer edits to keep this. Undo reverses it until then."
        )

    def _view_panel(self, canvas_point):
        project, point_xy, layers, infos, features = self._click_context(canvas_point)
        idf = self._idf_at(project, layers, features, point_xy, self._tolerance(project))
        if idf is None:
            raise RuntimeError("Click an IDF to view a patch panel.")
        panels = panels_on_idf(feature_id(idf) or "", features)
        if not panels:
            raise RuntimeError(f"{feature_id(idf)} has no patch panels.")
        chooser = ChooseAssetDialog(
            "View patch panel",
            "Which patch panel would you like to view?",
            [(choice_label(feature_id(panel), (panel.get("values") or {}).get("name"), feature_id(idf)), panel) for panel in panels],
            self.iface.mainWindow(),
        )
        if chooser.exec_() != chooser.Accepted:
            return
        panel = chooser.selected()
        if panel is None:
            return
        tolerance = self._tolerance(project)
        tables = read_tables(layers, infos)
        inventory = panel_ports(panel, features, tables)
        for port in inventory["ports"]:
            if not port["occupied"]:
                port["detail"] = f"Port {port['number']} is open."
                continue
            lines = [f"Port {port['number']}"]
            if port.get("device_id"):
                lines.append(f"Device: {port['device_id']}")
            if port.get("cable_id"):
                span = port.get("cable_span") or ""
                if span and span != str(port["number"]):
                    lines.append(f"Cable: {port['cable_id']} (ports {span})")
                else:
                    lines.append(f"Cable: {port['cable_id']}")
            if port.get("switch_id"):
                lines.append(f"Switch: {local_asset_id(port['switch_id'], feature_id(idf))}")
            if port.get("switch_port_id"):
                lines.append(f"Switch port: {port['switch_port_id']}")
            seed = port.get("device") or port.get("cable")
            if seed is not None:
                traced = trace_selection([seed], features, tables, tolerance)
                lines.append("")
                lines.append(format_report(traced))
            port["detail"] = "\n".join(lines)
        inventory["title"] = choice_label(feature_id(panel), (panel.get("values") or {}).get("name"), feature_id(idf))
        PanelViewDialog(inventory, self.iface.mainWindow()).exec_()

    def _view_switch(self, canvas_point):
        project, point_xy, layers, infos, features = self._click_context(canvas_point)
        idf = self._idf_at(project, layers, features, point_xy, self._tolerance(project))
        if idf is None:
            raise RuntimeError("Click an IDF to view a switch.")
        tables = read_tables(layers, infos)
        switches = switches_for_idf(feature_id(idf) or "", tables.get("switches") or [])
        if not switches:
            raise RuntimeError(f"{feature_id(idf)} has no switches.")
        chooser = ChooseAssetDialog(
            "View switch",
            "Which switch would you like to view?",
            [(choice_label(row.get("sw_id"), row.get("sw_name"), feature_id(idf)), row) for row in switches],
            self.iface.mainWindow(),
        )
        if chooser.exec_() != chooser.Accepted:
            return
        switch = chooser.selected()
        if not switch:
            return
        inventory = switch_inventory(switch, tables.get("switch_ports") or [], tables.get("patches") or [])
        for port in inventory["ports"]:
            if not port["occupied"]:
                port["detail"] = f"{port['switch_port_id']} is open."
                continue
            lines = [port["switch_port_id"]]
            if port.get("switch_port_name") and port["switch_port_name"].casefold() != port["switch_port_id"].casefold():
                lines.append(f"Name: {port['switch_port_name']}")
            if port.get("panel_id"):
                lines.append(f"Patch panel: {local_asset_id(port['panel_id'], feature_id(idf))}")
            if port.get("panel_port"):
                lines.append(f"Panel port: {port['panel_port']}")
            panel = next((feature for feature in features if feature_id(feature) == port.get("panel_id")), None)
            wanted = set(parse_range(port.get("panel_port")))
            if panel is not None and wanted:
                seen = set()
                for item in panel_ports(panel, features, tables)["ports"]:
                    if item["number"] not in wanted:
                        continue
                    pair = (item.get("device_id") or "", item.get("cable_id") or "")
                    if pair in seen or pair == ("", ""):
                        continue
                    seen.add(pair)
                    if item.get("device_id"):
                        lines.append(f"Device: {item['device_id']}")
                    if item.get("cable_id"):
                        lines.append(f"Cable: {item['cable_id']}")
            port["detail"] = "\n".join(lines)
        PanelViewDialog(inventory, self.iface.mainWindow()).exec_()

    def _run(self, title, function) -> None:
        try:
            function()
        except Cancelled:
            return
        except Exception as exc:
            QgsMessageLog.logMessage(traceback.format_exc(), "OSP Connectivity", Qgis.Critical)
            QMessageBox.critical(self.iface.mainWindow(), title, str(exc))

    def _action(self, text, slot) -> None:
        icons = {
            "Build associations": "build", "Associate selection": "associate", "Modify associations": "modify",
            "Trace selection": "trace", "Add fiber enclosure": "enclosure", "Add patch panel": "panel",
            "View patch panel": "view-panel", "View switch": "view-switch", "Add switch": "switch",
            "Patching": "patch", "Add device": "device", "Add cable": "cable", "Set snap tolerance": "snap",
            "Splice Editor": "splice-editor",
        }
        path = Path(__file__).parent / "icons" / f"{icons[text]}.svg"
        action = QAction(QIcon(str(path)), text, self.iface.mainWindow())
        action.setToolTip(text)
        action.setStatusTip(text)
        action.triggered.connect(slot)
        self.toolbar.addAction(action)
        button = self.toolbar.widgetForAction(action)
        if button is not None:
            button.setFixedSize(32, 32)
            button.setAccessibleName(text)
        self.iface.addPluginToMenu("OSP Connectivity", action)
        self.actions.append(action)


class Cancelled(Exception):
    """The enclosure dialog was closed without saving."""
