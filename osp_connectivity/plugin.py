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
    apply_edits,
    apply_inserts,
    apply_plain_edits,
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
from .core.fields import enclosure_id, media_of, next_id, parse_count
from .core.geometry import distance, split_linestring
from .core.link import RowInsert, plan_links
from .core.strands import build_splice_rows
from .core.trace import format_report, trace_selection
from .dialogs import AssociateDialog, ExportDialog, SpliceDialog


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
        errors = session.commit()
        lines = list(result.get("warnings") or [])
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
        # The click results were collected before ensure_project. Fids are unchanged.
        enclosure = self._next_enclosure_id(features, through[0][0], store)
        strand_keys = existing_keys(features, read_tables(layers, infos))["strands"]
        session = EditSession()
        try:
            add_enclosure_point(session, project, store["splice_enc"], point_xy, enclosure)
            created_segments = []
            reserved = []
            for feature, result in through:
                known = self._ids_on_layer(features, feature["layer_id"]) + reserved
                new_id = next_id(known, feature_id(feature) or "OSP-0000")
                reserved.append(new_id)
                split_cable(session, project, layers, infos, feature, result, new_id, enclosure)
                created_segments.append((feature, new_id))
                self._membership(session, store, enclosure, feature)
                self._membership_values(session, store, enclosure, feature["layer_id"], new_id)
            for feature, _result in laterals:
                self._membership(session, store, enclosure, feature)
                self._label_near_end(session, layers, infos, feature, point_xy, enclosure)
            for feature, new_id in created_segments:
                self._splice_pair(session, store, feature, new_id, laterals, enclosure, strand_keys)
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
            f"Enclosure {enclosure} was added. Express strands were written, and any fusion ranges you entered were saved.\n"
            "Save layer edits to keep this. Undo reverses it until then."
        )

    def _splice_pair(self, session, store, feature, new_id, laterals, enclosure, strand_keys) -> None:
        count = parse_count((feature.get("values") or {}).get("strand_count")) or 0
        targets = [{"id": new_id, "layer_id": feature["layer_id"]}]
        for lateral, _result in laterals:
            targets.append({"id": feature_id(lateral), "layer_id": lateral["layer_id"]})
        dialog = SpliceDialog(count or 12, feature_id(feature) or "", targets, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            raise Cancelled()
        edits = dialog.result_edits()
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

    def _next_enclosure_id(self, features, cable, store) -> str:
        existing = [feature_id(feature) for feature in features if feature.get("role") == "splice_enc" and feature_id(feature)]
        layer = store.get("splice_enc")
        template_style = bool(layer is not None and "osp_links.gpkg" not in (layer.source() or ""))
        return enclosure_id(existing, feature_id(cable) or "OSP-0001", template_style)

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
