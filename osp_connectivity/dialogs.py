"""Dialogs for associating a selection and for editing a splice."""

from __future__ import annotations

import re

from qgis.PyQt.QtCore import QDate, QDateTime, Qt, QTime, QVariant
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core.associate import port_in_use
from .core.catalog import feature_id
from .core.handholes import handhole_label
from .core.export import COLUMNS, item_label
from .core.fields import parse_count, parse_range
from .core.place import name_taken
from .core.naming import local_asset_id, scoped_asset_id
from .core.strands import build_splice_rows, compress_splices, validate_edits
from .core.splice_editor import group_rows, strict_range, validate_groups
from .core.switches import (
    new_patch,
    open_panel_ports,
    open_switch_ports,
    panels_for_idf,
    patch_line,
    switches_for_idf,
)


def _panel_key(combo, idf):
    text = combo.currentText().strip()
    if combo.currentIndex() >= 0 and text == combo.itemText(combo.currentIndex()) and combo.currentData():
        return combo.currentData()
    return scoped_asset_id(text, idf.currentData() or idf.currentText())


class AssociateDialog(QDialog):
    def __init__(self, proposal, occupied, parent=None):
        super().__init__(parent)
        self.proposal = proposal
        self.occupied = occupied
        self.setWindowTitle("Associate selection")
        self.resize(460, 520)
        layout = QVBoxLayout(self)

        summary = [
            f"Media: {proposal.media}",
            f"Cables: {', '.join(feature_id(item) or '?' for item in proposal.cables) or 'none'}",
            f"Devices: {', '.join(feature_id(item) or '?' for item in proposal.devices) or 'none'}",
            f"IDFs: {', '.join(feature_id(item) or '?' for item in proposal.idfs) or 'none'}",
        ]
        if proposal.structures:
            summary.append("Pole: " + ", ".join(feature_id(item) or "?" for item in proposal.structures))
        layout.addWidget(QLabel("\n".join(summary)))

        if proposal.warnings:
            warning = QLabel("\n".join(proposal.warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)

        form = QFormLayout()
        self.a_end = QComboBox()
        self.z_end = QComboBox()
        for box in (self.a_end, self.z_end):
            box.addItems(["device", "idf"])
        if proposal.convention == "a_idf":
            self.a_end.setCurrentText("idf")
            self.z_end.setCurrentText("device")
        else:
            self.a_end.setCurrentText("device")
            self.z_end.setCurrentText("idf")
        form.addRow("A end", self.a_end)
        form.addRow("Z end", self.z_end)

        self.idf = QComboBox()
        for item in proposal.idfs:
            self.idf.addItem(feature_id(item) or "", feature_id(item))
        form.addRow("IDF", self.idf)

        self.panel = QComboBox()
        self.panel.setEditable(True)
        form.addRow("Panel", self.panel)
        self.port = QLineEdit()
        form.addRow("Port", self.port)

        self.strands = QLineEdit()
        self.strands.setPlaceholderText("1-2")
        form.addRow("Fiber strands", self.strands)
        self.strands.setVisible(proposal.media == "fiber")
        layout.addLayout(form)
        self._refill_panels()
        self.idf.currentIndexChanged.connect(self._refill_panels)

        layout.addWidget(QLabel("Duct order"))
        self.ducts = QListWidget()
        for duct in proposal.ducts:
            label = feature_id(duct) or f"fid {duct.get('fid')}"
            self.ducts.addItem(label)
            self.ducts.item(self.ducts.count() - 1).setData(Qt.UserRole, duct)
        layout.addWidget(self.ducts)
        buttons = QHBoxLayout()
        up = QPushButton("Up")
        down = QPushButton("Down")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        layout.addLayout(buttons)

        layout.addWidget(QLabel("Handholes on this cable"))
        self.handholes = QListWidget()
        for handhole in proposal.handholes:
            label = handhole_label(handhole)
            if not label:
                continue
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, handhole)
            self.handholes.addItem(item)
        self.handholes.setVisible(self.handholes.count() > 0)
        layout.addWidget(self.handholes)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        if not proposal.ready:
            box.button(QDialogButtonBox.Ok).setEnabled(False)
        layout.addWidget(box)

    def _move(self, direction: int) -> None:
        row = self.ducts.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= self.ducts.count():
            return
        item = self.ducts.takeItem(row)
        self.ducts.insertItem(target, item)
        self.ducts.setCurrentRow(target)

    def _accept(self) -> None:
        panel = _panel_key(self.panel, self.idf)
        port = self.port.text().strip()
        if self.proposal.idfs and (not panel or not port):
            QMessageBox.warning(self, "Associate selection", "Enter the panel and the port.")
            return
        if panel and port and port_in_use(self.occupied, panel, port):
            QMessageBox.warning(self, "Associate selection", f"{self.idf.currentText()} {local_asset_id(panel, self.idf.currentData())}:{port} is already used.")
            return
        if self.proposal.media == "fiber" and not parse_range(self.strands.text()):
            QMessageBox.warning(self, "Associate selection", "Enter the fiber strand numbers, such as 1-2.")
            return
        self.accept()

    def choices(self) -> dict:
        ducts = []
        for row in range(self.ducts.count()):
            ducts.append(self.ducts.item(row).data(Qt.UserRole))
        self.proposal.ducts = ducts
        handholes = []
        for row in range(self.handholes.count()):
            item = self.handholes.item(row)
            if item.checkState() == Qt.Checked:
                handholes.append(item.data(Qt.UserRole))
        return {
            "a_role": self.a_end.currentText(),
            "z_role": self.z_end.currentText(),
            "idf_id": self.idf.currentData() or self.idf.currentText(),
            "panel_id": _panel_key(self.panel, self.idf),
            "port": self.port.text().strip(),
            "strands": self.strands.text().strip(),
            "handholes": handholes,
        }

    def _refill_panels(self) -> None:
        current = self.panel.currentText().strip()
        idf_id = self.idf.currentData() or self.idf.currentText()
        names = self.proposal.panels_by_idf.get(idf_id, self.proposal.panels)
        self.panel.blockSignals(True)
        self.panel.clear()
        for name in names:
            self.panel.addItem(local_asset_id(name, idf_id), name)
        if current:
            self.panel.setCurrentText(current)
        self.panel.blockSignals(False)


class SpliceDialog(QDialog):
    def __init__(
        self,
        strand_count: int,
        from_cable: str,
        targets: list[dict],
        parent=None,
        prefix: str = "OSP",
        prefix_enabled: bool = True,
        title: str = "Fiber enclosure",
        prefix_label: str = "Enclosure prefix",
        note: str = "",
        initial_fusions: list | None = None,
        initial_opens: str = "",
    ):
        super().__init__(parent)
        self.from_cable = from_cable
        self.targets = targets
        self.setWindowTitle(title)
        self.resize(560, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Strands are expressed through on the same numbers. "
                "Add a fusion row only for the strands that are spliced to another cable."
            )
        )
        if note:
            notice = QLabel(note)
            notice.setWordWrap(True)
            layout.addWidget(notice)
        form = QFormLayout()
        self.prefix = QLineEdit(prefix or "OSP")
        self.prefix.setPlaceholderText("VIB")
        self.prefix.setEnabled(prefix_enabled)
        form.addRow(prefix_label, self.prefix)
        self.count = QSpinBox()
        self.count.setRange(1, 864)
        self.count.setValue(max(1, strand_count or 1))
        self.count.valueChanged.connect(self._preview)
        form.addRow("Strand count", self.count)
        self.opens = QLineEdit(initial_opens or "")
        self.opens.setPlaceholderText("Optional open ranges, such as 10-12")
        self.opens.textChanged.connect(self._preview)
        form.addRow("Left open", self.opens)
        layout.addLayout(form)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["From", "To", "To cable", "Start strand"])
        self.table.itemChanged.connect(self._preview)
        layout.addWidget(self.table)
        add = QPushButton("Add fusion")
        add.clicked.connect(self._add_row)
        layout.addWidget(add)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._load_fusions(initial_fusions or [])
        self._preview()

    def _load_fusions(self, fusions: list[dict]) -> None:
        self.table.blockSignals(True)
        for fusion in fusions:
            self._add_row()
            row = self.table.rowCount() - 1
            self.table.item(row, 0).setText(str(fusion.get("a_from") or ""))
            self.table.item(row, 1).setText(str(fusion.get("a_to") or ""))
            self.table.item(row, 3).setText(str(fusion.get("b_start") or ""))
            combo = self.table.cellWidget(row, 2)
            wanted = str(fusion.get("b_cable_id") or "").casefold()
            if combo is not None:
                combo.blockSignals(True)
                for index in range(combo.count()):
                    data = combo.itemData(index)
                    if data and str(data.get("id") or "").casefold() == wanted:
                        combo.setCurrentIndex(index)
                        break
                combo.blockSignals(False)
        self.table.blockSignals(False)

    def _add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem("1"))
        self.table.setItem(row, 1, QTableWidgetItem("1"))
        combo = QComboBox()
        for target in self.targets:
            combo.addItem(target.get("label") or target["id"], target)
        combo.currentIndexChanged.connect(self._preview)
        self.table.setCellWidget(row, 2, combo)
        self.table.setItem(row, 3, QTableWidgetItem("1"))

    def _fusions(self) -> list[dict]:
        fusions = []
        for row in range(self.table.rowCount()):
            start = parse_count(self.table.item(row, 0).text() if self.table.item(row, 0) else None)
            end = parse_count(self.table.item(row, 1).text() if self.table.item(row, 1) else None)
            begin = parse_count(self.table.item(row, 3).text() if self.table.item(row, 3) else None)
            combo = self.table.cellWidget(row, 2)
            target = combo.currentData() if combo else None
            if not start or not end or not begin or not target:
                continue
            fusions.append(
                {
                    "a_from": start,
                    "a_to": end,
                    "b_cable_id": target["id"],
                    "b_layer_id": target.get("layer_id") or "",
                    "b_start": begin,
                }
            )
        return fusions

    def _open_ranges(self) -> list[dict]:
        numbers = parse_range(self.opens.text())
        if not numbers:
            return []
        ranges = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append({"a_from": start, "a_to": previous})
            start = previous = number
        ranges.append({"a_from": start, "a_to": previous})
        return ranges

    def _preview(self, *_args) -> None:
        other = self.targets[0]["id"] if self.targets else ""
        rows = build_splice_rows(
            self.count.value(),
            self.from_cable,
            other,
            "enclosure",
            self._fusions(),
            self._open_ranges(),
        )
        self.preview.setText("\n".join(compress_splices(rows)) or "No strands.")

    def _accept(self) -> None:
        if not self.prefix.text().strip():
            QMessageBox.warning(self, "Fiber enclosure", "Enter an enclosure prefix, such as VIB or OSP.")
            return
        # A malformed row must never disappear silently from a saved splice map.
        for row in range(self.table.rowCount()):
            for column in (0, 1, 3):
                item = self.table.item(row, column)
                if item is None or not item.text().strip().isdigit() or int(item.text().strip()) < 1:
                    QMessageBox.warning(self, "Fiber enclosure", f"Fusion row {row + 1}: enter positive fiber numbers.")
                    return
            if not self.table.cellWidget(row, 2).currentData():
                QMessageBox.warning(self, "Fiber enclosure", f"Fusion row {row + 1}: choose a target cable.")
                return
        try:
            for interval in re.split(r"[,;]+", self.opens.text()):
                if interval.strip():
                    strict_range(interval)
        except ValueError as exc:
            QMessageBox.warning(self, "Fiber enclosure", str(exc))
            return
        problems = validate_edits(self.count.value(), self._fusions(), self._open_ranges())
        if not problems and self.targets:
            rows = build_splice_rows(self.count.value(), self.from_cable, self.targets[0]["id"], "enclosure",
                                     self._fusions(), self._open_ranges())
            cables = [{"id": self.from_cable, "capacity": self.count.value()}]
            for target in self.targets:
                capacity = self.count.value() if target.get("split_segment") else target.get("capacity", 0)
                cables.append({"id": target["id"], "layer_id": target.get("layer_id") or "", "capacity": capacity})
            problems.extend(validate_groups(group_rows(rows), cables))
        if problems:
            QMessageBox.warning(self, "Fiber enclosure", "\n".join(problems))
            return
        self.accept()

    def result_edits(self) -> dict:
        return {
            "strand_count": self.count.value(),
            "fusions": self._fusions(),
            "opens": self._open_ranges(),
            "prefix": self.prefix.text().strip(),
        }


class ModifyDialog(QDialog):
    """Change the panel, port, and handholes already tied to one device."""

    def __init__(self, association, occupied, parent=None):
        super().__init__(parent)
        self.association = association
        self.occupied = occupied or []
        device = feature_id(association.get("device")) or "device"
        self.setWindowTitle("Modify associations")
        self.resize(460, 560)
        layout = QVBoxLayout(self)
        cables = ", ".join(feature_id(item) or "?" for item in association.get("cables") or []) or "none"
        layout.addWidget(QLabel(f"Device: {device}\nCables: {cables}"))
        if association.get("warnings"):
            layout.addWidget(QLabel("\n".join(association["warnings"])))
        form = QFormLayout()
        self.idf = QComboBox()
        for item in association.get("idfs") or []:
            self.idf.addItem(feature_id(item) or "", feature_id(item))
        form.addRow("IDF", self.idf)
        self.panel = QComboBox()
        self.panel.setEditable(True)
        form.addRow("Panel", self.panel)
        self.port = QLineEdit(association.get("port") or "")
        form.addRow("Port", self.port)
        self.strands = QLineEdit()
        self.strands.setPlaceholderText("1-2")
        if association.get("media") == "fiber":
            form.addRow("Fiber strands", self.strands)
        layout.addLayout(form)
        self._refill_panels()
        self.idf.currentIndexChanged.connect(self._refill_panels)
        if association.get("panel"):
            self.panel.setCurrentText(local_asset_id(association["panel"], self.idf.currentData()))
        layout.addWidget(QLabel("Handholes"))
        self.handholes = QListWidget()
        checked = {
            (item.get("layer_id"), item.get("fid"))
            for item in association.get("checked") or []
        }
        for handhole in association.get("handholes") or []:
            label = handhole_label(handhole)
            if not label:
                continue
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if (handhole.get("layer_id"), handhole.get("fid")) in checked else Qt.Unchecked)
            item.setData(Qt.UserRole, handhole)
            self.handholes.addItem(item)
        layout.addWidget(self.handholes)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        if not association.get("ready"):
            buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        layout.addWidget(buttons)

    def _refill_panels(self) -> None:
        current = self.panel.currentText().strip()
        idf_id = self.idf.currentData() or self.idf.currentText()
        names = (self.association.get("panels_by_idf") or {}).get(idf_id) or []
        self.panel.blockSignals(True)
        self.panel.clear()
        for name in names:
            self.panel.addItem(local_asset_id(name, idf_id), name)
        if current:
            self.panel.setCurrentText(current)
        elif self.association.get("panel"):
            self.panel.setCurrentText(local_asset_id(self.association["panel"], idf_id))
        self.panel.blockSignals(False)

    def _accept(self) -> None:
        panel = _panel_key(self.panel, self.idf)
        port = self.port.text().strip()
        if self.association.get("idfs") and (not panel or not port):
            QMessageBox.warning(self, "Modify associations", "Enter the panel and the port.")
            return
        if panel and port and port_in_use(self.occupied, panel, port):
            QMessageBox.warning(self, "Modify associations", f"{self.idf.currentText()} {local_asset_id(panel, self.idf.currentData())}:{port} is already used.")
            return
        if self.association.get("media") == "fiber" and self.strands.text().strip() and not parse_range(self.strands.text()):
            QMessageBox.warning(self, "Modify associations", "Enter the fiber strand numbers, such as 1-2.")
            return
        self.accept()

    def choices(self) -> dict:
        handholes = []
        for row in range(self.handholes.count()):
            item = self.handholes.item(row)
            if item.checkState() == Qt.Checked:
                handholes.append(item.data(Qt.UserRole))
        return {
            "idf_id": self.idf.currentData() or self.idf.currentText(),
            "panel_id": _panel_key(self.panel, self.idf),
            "port": self.port.text().strip(),
            "strands": self.strands.text().strip(),
            "handholes": handholes,
        }


class CircuitCardDialog(QDialog):
    """Choose which traced circuits become printable cards."""

    def __init__(self, reports, parent=None):
        super().__init__(parent)
        self.reports = list(reports)
        self.setWindowTitle("Circuit card")
        self.resize(420, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Check the circuits to print. Each one is a page in the PDF."))
        self.items = QListWidget()
        for index, report in enumerate(self.reports):
            item = QListWidgetItem(item_label(report, index))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, index)
            self.items.addItem(item)
        layout.addWidget(self.items)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choices(self) -> dict:
        reports = []
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.checkState() == Qt.Checked:
                reports.append(self.reports[item.data(Qt.UserRole)])
        return {"reports": reports}

    def _accept(self) -> None:
        if not self.choices()["reports"]:
            QMessageBox.warning(self, "Circuit card", "Select at least one circuit.")
            return
        self.accept()


class ExportDialog(QDialog):
    """Choose which traced circuits and columns go into a CSV or Excel file."""

    def __init__(self, reports, parent=None):
        super().__init__(parent)
        self.reports = list(reports)
        self.setWindowTitle("Export report")
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        description = QLabel("One row per circuit, with only its strand path and route assets. Include Link ID to update matching links later. Blank QGIS tracking values preserve entries in an existing tracker.")
        description.setWordWrap(True)
        layout.addWidget(description)

        lists = QHBoxLayout()
        self.items = QListWidget()
        for index, report in enumerate(self.reports):
            item = QListWidgetItem(item_label(report, index))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, index)
            self.items.addItem(item)
        self.fields = QListWidget()
        for name in COLUMNS:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if name == 'Splices' else Qt.Checked)
            item.setData(Qt.UserRole, name)
            self.fields.addItem(item)
        lists.addWidget(self._boxed("Items", self.items))
        lists.addWidget(self._boxed("Fields", self.fields))
        layout.addLayout(lists)

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Create a new file", "new")
        self.mode.addItem("Update matching links in an existing tracker", "update")
        self.mode.addItem("Add rows to an existing file", "append")
        self.format = QComboBox()
        self.format.addItem("Excel (.xlsx)", "xlsx")
        self.format.addItem("CSV (.csv)", "csv")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("File", self.mode)
        form.addRow("Format", self.format)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choices(self) -> dict:
        reports = []
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.checkState() == Qt.Checked:
                reports.append(self.reports[item.data(Qt.UserRole)])
        fields = []
        for index in range(self.fields.count()):
            item = self.fields.item(index)
            if item.checkState() == Qt.Checked:
                fields.append(item.data(Qt.UserRole))
        return {
            "reports": reports,
            "fields": fields,
            "mode": self.mode.currentData(),
            "format": self.format.currentData(),
        }

    def _boxed(self, title, widget):
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        inner.addWidget(widget)
        return box

    def _mode_changed(self) -> None:
        self.format.setEnabled(self.mode.currentData() == "new")

    def _accept(self) -> None:
        choice = self.choices()
        if not choice["reports"]:
            QMessageBox.warning(self, "Export report", "Select at least one item.")
            return
        if not choice["fields"]:
            QMessageBox.warning(self, "Export report", "Select at least one field.")
            return
        if choice['mode'] == 'update' and 'Link ID' not in choice['fields']:
            QMessageBox.warning(self, 'Export report', 'Include Link ID to update matching links.')
            return
        self.accept()


class PatchPanelDialog(QDialog):
    """Name the panel added on an IDF. Copper and fiber cables are named separately."""

    def __init__(self, idf_id: str, groups: list[dict], suggestions: dict, taken, occupied, parent=None):
        super().__init__(parent)
        self.idf_id = idf_id
        self.groups = groups or []
        self.suggestions = suggestions or {}
        self.taken = list(taken or [])
        self.occupied = occupied or []
        self.rows = []
        self.setWindowTitle("Add patch panel")
        self.resize(460, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"IDF: {idf_id}"))
        if not self.groups:
            form = QFormLayout()
            self.panel = QLineEdit(self.suggestions.get("copper") or "")
            self._suggested = self.panel.text()
            form.addRow("Panel id", self.panel)
            self.media = QComboBox()
            self.media.addItem("Copper", "copper")
            self.media.addItem("Fiber", "fiber")
            self.media.currentIndexChanged.connect(self._follow_media)
            form.addRow("Panel", self.media)
            layout.addLayout(form)
        else:
            self.panel = None
            for group in self.groups:
                title = "Fiber" if group.get("media") == "fiber" else "Copper"
                box = QGroupBox(title)
                form = QFormLayout(box)
                cables = ", ".join(group.get("cable_ids") or []) or "none"
                summary = f"Cables: {cables}"
                if group.get("device_id"):
                    summary += f"\nDevice: {group['device_id']}"
                form.addRow(QLabel(summary))
                panel = QLineEdit(self.suggestions.get(group.get("media")) or "")
                form.addRow("Panel id", panel)
                a_end = QComboBox()
                z_end = QComboBox()
                for combo in (a_end, z_end):
                    combo.addItems(["device", "idf"])
                if group.get("idf_end") == "a_end":
                    a_end.setCurrentText("idf")
                    z_end.setCurrentText("device")
                else:
                    a_end.setCurrentText("device")
                    z_end.setCurrentText("idf")
                form.addRow("A end", a_end)
                form.addRow("Z end", z_end)
                port = QLineEdit()
                form.addRow("Port", port)
                strands = QLineEdit()
                strands.setPlaceholderText("1-2")
                if group.get("media") == "fiber":
                    form.addRow("Fiber strands", strands)
                self.rows.append({"panel": panel, "a_end": a_end, "z_end": z_end, "port": port, "strands": strands})
                layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _follow_media(self) -> None:
        media = self.media.currentData()
        suggested = self.suggestions.get(media) or ""
        if self.panel.text().strip() == (self._suggested or ""):
            self.panel.setText(suggested)
        self._suggested = suggested

    def _accept(self) -> None:
        names = []
        if not self.groups:
            panel = scoped_asset_id(self.panel.text().strip(), self.idf_id)
            if not panel:
                QMessageBox.warning(self, "Add patch panel", "Enter a panel id.")
                return
            if name_taken(panel, self.taken):
                QMessageBox.warning(self, "Add patch panel", f"{local_asset_id(panel, self.idf_id)} is already used on {self.idf_id}. Choose a different name.")
                return
            self.accept()
            return
        for row, group in zip(self.rows, self.groups):
            panel = scoped_asset_id(row["panel"].text().strip(), self.idf_id)
            port = row["port"].text().strip()
            label = "Fiber" if group.get("media") == "fiber" else "Copper"
            if not panel:
                QMessageBox.warning(self, "Add patch panel", f"Enter the {label.lower()} panel id.")
                return
            if name_taken(panel, self.taken) or name_taken(panel, names):
                QMessageBox.warning(self, "Add patch panel", f"{local_asset_id(panel, self.idf_id)} is already used on {self.idf_id}. Choose a different name.")
                return
            if not port:
                QMessageBox.warning(self, "Add patch panel", f"Enter the port for {self.idf_id} {local_asset_id(panel, self.idf_id)}.")
                return
            if port_in_use(self.occupied, panel, port):
                QMessageBox.warning(self, "Add patch panel", f"{self.idf_id} {local_asset_id(panel, self.idf_id)}:{port} is already used.")
                return
            if group.get("media") == "fiber" and not parse_range(row["strands"].text()):
                QMessageBox.warning(self, "Add patch panel", "Enter the fiber strand numbers, such as 1-2.")
                return
            names.append(panel)
        self.accept()

    def choices(self) -> list[dict]:
        if not self.groups:
            return [{"panel_id": scoped_asset_id(self.panel.text().strip(), self.idf_id), "media": self.media.currentData(), "associate": False}]
        chosen = []
        for row, group in zip(self.rows, self.groups):
            chosen.append(
                {
                    "panel_id": scoped_asset_id(row["panel"].text().strip(), self.idf_id),
                    "media": group.get("media"),
                    "associate": True,
                    "a_role": row["a_end"].currentText(),
                    "z_role": row["z_end"].currentText(),
                    "port": row["port"].text().strip(),
                    "strands": row["strands"].text().strip(),
                }
            )
        return chosen


class PlacementDialog(QDialog):
    """Name a new device or cable, and record the association when it lands on existing plant."""

    def __init__(self, title, kind_label, kinds, name_label, suggested_name, taken, context, occupied, parent=None):
        super().__init__(parent)
        self.context = context or {}
        self.occupied = occupied or []
        self.taken = taken or []
        self.setWindowTitle(title)
        self.resize(460, 520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for item in kinds:
            self.kind.addItem(item["label"], item)
        form.addRow(kind_label, self.kind)
        self.name = QLineEdit(suggested_name or "")
        self.name.setPlaceholderText("Required, and it must be unique")
        form.addRow(name_label, self.name)
        self.media = QComboBox()
        self.media.addItem("Copper", "copper")
        self.media.addItem("Fiber", "fiber")
        if self.context.get("media") == "fiber":
            self.media.setCurrentIndex(1)
        if self.context.get("ask_media"):
            form.addRow("Media", self.media)
        layout.addLayout(form)

        self.a_end = QComboBox()
        self.z_end = QComboBox()
        self.idf = QComboBox()
        self.panel = QComboBox()
        self.panel.setEditable(True)
        self.port = QLineEdit()
        self.strands = QLineEdit()
        self.strands.setPlaceholderText("1-2")
        self.association = QWidget()
        association_layout = QVBoxLayout(self.association)
        association_layout.setContentsMargins(0, 0, 0, 0)
        summary = self.context.get("summary") or ""
        if summary:
            association_layout.addWidget(QLabel(summary))
        fields = QFormLayout()
        for box in (self.a_end, self.z_end):
            box.addItems(["device", "idf"])
        if self.context.get("device_end") == "z_end":
            self.a_end.setCurrentText("idf")
            self.z_end.setCurrentText("device")
        else:
            self.a_end.setCurrentText("device")
            self.z_end.setCurrentText("idf")
        fields.addRow("A end", self.a_end)
        fields.addRow("Z end", self.z_end)
        for item in self.context.get("idfs") or []:
            self.idf.addItem(feature_id(item) or "", feature_id(item))
        fields.addRow("IDF", self.idf)
        fields.addRow("Panel", self.panel)
        fields.addRow("Port", self.port)
        fields.addRow("Fiber strands", self.strands)
        association_layout.addLayout(fields)
        layout.addWidget(self.association)
        self.association.setVisible(bool(self.context.get("associate")))
        self._refill_panels()
        self.idf.currentIndexChanged.connect(self._refill_panels)
        self.media.currentIndexChanged.connect(self._media_changed)
        self._media_changed()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choices(self) -> dict:
        kind = self.kind.currentData() or {}
        return {
            "layer_id": kind.get("layer_id"),
            "role": kind.get("role"),
            "name": self.name.text().strip(),
            "media": self.media.currentData(),
            "associate": bool(self.context.get("associate")),
            "a_role": self.a_end.currentText(),
            "z_role": self.z_end.currentText(),
            "idf_id": self.idf.currentData() or self.idf.currentText(),
            "panel_id": _panel_key(self.panel, self.idf),
            "port": self.port.text().strip(),
            "strands": self.strands.text().strip(),
        }

    def _refill_panels(self) -> None:
        current = self.panel.currentText().strip()
        idf_id = self.idf.currentData() or self.idf.currentText()
        names = (self.context.get("panels_by_idf") or {}).get(idf_id) or []
        preferred = self.context.get("panel_id") or ""
        self.panel.blockSignals(True)
        self.panel.clear()
        for name in names:
            self.panel.addItem(local_asset_id(name, idf_id), name)
        if current:
            self.panel.setCurrentText(current)
        elif preferred:
            self.panel.setCurrentText(local_asset_id(preferred, idf_id))
        self.panel.blockSignals(False)

    def _media_changed(self) -> None:
        show_strands = self.media.currentData() == "fiber" and bool(self.context.get("associate"))
        self.strands.setVisible(show_strands)

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, self.windowTitle(), "Enter a name.")
            return
        if name_taken(name, self.taken):
            QMessageBox.warning(self, self.windowTitle(), f"{name} is already used. Choose a different name.")
            return
        if self.context.get("associate") and (self.context.get("idfs") or self.context.get("panel_id")):
            panel = _panel_key(self.panel, self.idf)
            port = self.port.text().strip()
            if not panel or not port:
                QMessageBox.warning(self, self.windowTitle(), "Enter the panel and the port.")
                return
            if port_in_use(self.occupied, panel, port):
                QMessageBox.warning(self, self.windowTitle(), f"{self.idf.currentText()} {local_asset_id(panel, self.idf.currentData())}:{port} is already used.")
                return
            if self.media.currentData() == "fiber" and not parse_range(self.strands.text()):
                QMessageBox.warning(self, self.windowTitle(), "Enter the fiber strand numbers, such as 1-2.")
                return
        self.accept()


class BlankAttributeDialog(QDialog):
    """Ask for the layer attributes that an association did not already fill."""

    def __init__(self, layer_name: str, fields, parent=None, initials=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add {layer_name}")
        self.resize(480, 520)
        self._editors = []
        self._parsed = {}
        self._initials = initials or {}
        layout = QVBoxLayout(self)
        note = QLabel(
            "The id you entered is filled in. Cable, pole, panel, port, and end fields already determined are not listed. Leave a row blank to keep it empty."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        for field in fields:
            editor = self._editor(field, self._initials.get(field.name()))
            form.addRow(field.alias() or field.name(), editor)
            self._editors.append((field, editor))
        holder = QWidget()
        holder.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return dict(self._parsed)

    def _editor(self, field, initial=None):
        if field.type() == QVariant.Bool:
            box = QComboBox()
            box.addItem("", None)
            box.addItem("Yes", True)
            box.addItem("No", False)
            if initial is True:
                box.setCurrentIndex(1)
            elif initial is False:
                box.setCurrentIndex(2)
            return box
        edit = QLineEdit()
        if initial is not None and str(initial).strip() != "":
            edit.setText(str(initial))
        if field.type() in (QVariant.Int, QVariant.UInt, QVariant.LongLong, QVariant.ULongLong):
            edit.setPlaceholderText("Whole number")
        elif field.type() == QVariant.Double:
            edit.setPlaceholderText("Number")
        elif field.type() == QVariant.Date:
            edit.setPlaceholderText("YYYY-MM-DD")
        elif field.type() in (QVariant.DateTime, QVariant.Time):
            edit.setPlaceholderText("YYYY-MM-DD HH:MM")
        return edit

    def _accept(self) -> None:
        try:
            self._parsed = self._parse()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()

    def _parse(self) -> dict:
        parsed = {}
        for field, editor in self._editors:
            label = field.alias() or field.name()
            if isinstance(editor, QComboBox):
                value = editor.currentData()
                if value is not None:
                    parsed[field.name()] = value
                continue
            text = editor.text().strip()
            if not text:
                continue
            kind = field.type()
            if kind in (QVariant.Int, QVariant.UInt, QVariant.LongLong, QVariant.ULongLong):
                try:
                    parsed[field.name()] = int(text)
                except ValueError:
                    raise ValueError(f"{label} needs a whole number.") from None
            elif kind == QVariant.Double:
                try:
                    parsed[field.name()] = float(text)
                except ValueError:
                    raise ValueError(f"{label} needs a number.") from None
            elif kind == QVariant.Date:
                date = QDate.fromString(text, Qt.ISODate)
                if not date.isValid():
                    raise ValueError(f"{label} needs a date like 2026-10-16.")
                parsed[field.name()] = date
            elif kind == QVariant.DateTime:
                stamp = QDateTime.fromString(text, "yyyy-MM-dd HH:mm")
                if not stamp.isValid():
                    raise ValueError(f"{label} needs a date and time like 2026-10-16 14:30.")
                parsed[field.name()] = stamp
            elif kind == QVariant.Time:
                clock = QTime.fromString(text, "HH:mm")
                if not clock.isValid():
                    raise ValueError(f"{label} needs a time like 14:30.")
                parsed[field.name()] = clock
            else:
                parsed[field.name()] = text
        return parsed


class PortCountDialog(QDialog):
    """Ask how many ports a new patch panel has before it is created."""

    def __init__(self, panel_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Patch panel ports")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"How many ports does {panel_id} have?"))
        self.count = QSpinBox()
        self.count.setRange(1, 864)
        self.count.setValue(24)
        layout.addWidget(self.count)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def port_count(self) -> int:
        return int(self.count.value())


class ChooseAssetDialog(QDialog):
    """Pick one panel or switch that belongs to the IDF just clicked."""

    def __init__(self, title: str, prompt: str, choices: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 140)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(prompt))
        self.choice = QComboBox()
        for label, payload in choices:
            self.choice.addItem(label, payload)
        layout.addWidget(self.choice)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return self.choice.currentData()


class PanelViewDialog(QDialog):
    """A grid of one panel's ports. Occupied ports open the device and cable."""

    def __init__(self, inventory: dict, parent=None):
        super().__init__(parent)
        panel_id = inventory.get("title") or inventory.get("panel_id") or "Patch panel"
        self.setWindowTitle(str(panel_id))
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        idf = inventory.get("idf_id") or "not linked to an IDF"
        count = inventory.get("count") or 0
        used = sum(1 for port in inventory.get("ports") or [] if port.get("occupied"))
        layout.addWidget(QLabel(f"{panel_id}\nIDF: {idf}\n{used} of {count} ports occupied"))
        legend = QLabel(
            inventory.get("legend")
            or "Blue ports have a cable, device, or switch patch. Click one to see them."
        )
        legend.setWordWrap(True)
        layout.addWidget(legend)

        grid = QGridLayout()
        grid.setSpacing(4)
        columns = 8 if any(len(str(port.get("label") or "")) > 4 for port in inventory.get("ports") or []) else 12
        for index, port in enumerate(inventory.get("ports") or []):
            label = str(port.get("label") or port.get("number") or "")
            button = QPushButton(label)
            button.setFixedHeight(28)
            button.setMinimumWidth(44 if len(label) <= 3 else 78)
            if port.get("occupied"):
                button.setStyleSheet("QPushButton { background: #145ad2; color: white; border: 1px solid #0e3f96; }")
            else:
                button.setStyleSheet("QPushButton { background: #f4f1ea; color: #333; border: 1px solid #c8c2b4; }")
            button.clicked.connect(lambda _checked=False, item=port: self._show(item))
            row, column = divmod(index, columns)
            grid.addWidget(button, row, column)
        holder = QWidget()
        holder.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        self.detail = QLabel("Select a port.")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def _show(self, port: dict) -> None:
        self.detail.setText(port.get("detail") or f"Port {port.get('number')} is open.")


class SwitchDialog(QDialog):
    """Attributes for a switch placed on an IDF."""

    def __init__(self, suggested_id: str, taken: list, parent=None, idf_id=''):
        super().__init__(parent)
        self.taken = taken
        self.idf_id = idf_id
        self.setWindowTitle("Add switch")
        self.resize(420, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'IDF: {idf_id}'))
        form = QFormLayout()
        self.identifier = QLineEdit(suggested_id)
        form.addRow("Switch id", self.identifier)
        self.name = QLineEdit(suggested_id)
        self._previous_identifier = suggested_id
        self.identifier.textChanged.connect(self._follow_identifier)
        form.addRow("Name", self.name)
        self.count = QSpinBox()
        self.count.setRange(1, 512)
        self.count.setValue(24)
        form.addRow("Port count", self.count)
        self.make = QLineEdit()
        form.addRow("Make", self.make)
        self.model = QLineEdit()
        form.addRow("Model", self.model)
        self.rack = QLineEdit()
        form.addRow("Rack number", self.rack)
        self.ru = QLineEdit()
        form.addRow("RU number", self.ru)
        self.first_port = QLineEdit("gi1/0/1")
        form.addRow("First switch port", self.first_port)
        layout.addLayout(form)
        note = QLabel("Ports are numbered from the first id, such as gi1/0/1, gi1/0/2, gi1/0/3.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _follow_identifier(self, text):
        if self.name.text() == self._previous_identifier:
            self.name.setText(local_asset_id(text, self.idf_id))
        self._previous_identifier = text

    def _accept(self) -> None:
        identifier = scoped_asset_id(self.identifier.text().strip(), self.idf_id)
        if not identifier:
            QMessageBox.warning(self, "Add switch", "Enter a switch id, such as VIB-SW-0001.")
            return
        if identifier.casefold() in {item.casefold() for item in self.taken}:
            QMessageBox.warning(self, "Add switch", f"{local_asset_id(identifier, self.idf_id)} already exists on {self.idf_id}.")
            return
        if not self.name.text().strip():
            QMessageBox.warning(self, "Add switch", "Enter a switch name.")
            return
        self.accept()

    def choices(self) -> dict:
        return {
            "sw_id": scoped_asset_id(self.identifier.text().strip(), self.idf_id),
            "sw_name": self.name.text().strip(),
            "sw_port_count": int(self.count.value()),
            "make": self.make.text().strip(),
            "model": self.model.text().strip(),
            "rack_number": self.rack.text().strip(),
            "ru_number": self.ru.text().strip(),
            "first_port": self.first_port.text().strip() or "gi1/0/1",
        }


class PatchDialog(QDialog):
    """See the patches at an IDF and add a cross connect from a panel port to a switch port."""

    def __init__(self, idfs: list, features: list, tables: dict, parent=None):
        super().__init__(parent)
        self.idfs = idfs
        self.features = features
        self.tables = tables
        self.pending = []
        self.setWindowTitle("Patching")
        self.resize(640, 560)
        layout = QVBoxLayout(self)
        self.existing = QListWidget()
        layout.addWidget(QLabel("Patches at this IDF"))
        layout.addWidget(self.existing)
        form = QFormLayout()
        self.idf = QComboBox()
        for item in idfs:
            self.idf.addItem(feature_id(item) or "", feature_id(item))
        form.addRow("IDF", self.idf)
        self.media = QComboBox()
        self.media.addItem("Copper", "copper")
        self.media.addItem("Fiber", "fiber")
        form.addRow("Patch type", self.media)
        self.single = QCheckBox("Single fiber circuit")
        form.addRow("", self.single)
        self.panel = QComboBox()
        form.addRow("Patch panel", self.panel)
        self.port = QComboBox()
        form.addRow("Open port", self.port)
        self.switch = QComboBox()
        form.addRow("Switch", self.switch)
        self.switch_port = QComboBox()
        form.addRow("Switch port", self.switch_port)
        layout.addLayout(form)
        add = QPushButton("Add patch")
        add.clicked.connect(self._add)
        layout.addWidget(add)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.idf.currentIndexChanged.connect(self._refill)
        self.media.currentIndexChanged.connect(self._refill)
        self.single.toggled.connect(self._refill_ports)
        self.panel.currentIndexChanged.connect(self._refill_ports)
        self.switch.currentIndexChanged.connect(self._refill_switch_ports)
        self._refill()

    def patches(self) -> list[dict]:
        return list(self.pending)

    def _idf_id(self) -> str:
        return self.idf.currentData() or ""

    def _all_patches(self) -> list[dict]:
        return list(self.tables.get("patches") or []) + self.pending

    def _refill(self, *_args) -> None:
        idf_id = self._idf_id()
        fiber = self.media.currentData() == "fiber"
        self.single.setVisible(fiber)
        if not fiber:
            self.single.setChecked(False)
        self.existing.clear()
        for row in list(self.tables.get("patches") or []) + self.pending:
            if (row.get("mdf_idf_id") or "").casefold() == (idf_id or "").casefold():
                self.existing.addItem(patch_line(row))
        current_panel = self.panel.currentData()
        self.panel.blockSignals(True)
        self.panel.clear()
        for feature in panels_for_idf(idf_id, self.features, "fiber" if fiber else "copper"):
            self.panel.addItem(local_asset_id(feature_id(feature), idf_id), feature)
        if current_panel is not None:
            for index in range(self.panel.count()):
                if feature_id(self.panel.itemData(index) or {}) == feature_id(current_panel):
                    self.panel.setCurrentIndex(index)
                    break
        self.panel.blockSignals(False)
        current_switch = self.switch.currentData()
        self.switch.blockSignals(True)
        self.switch.clear()
        for row in switches_for_idf(idf_id, self.tables.get("switches") or []):
            label = local_asset_id(row.get("sw_name") or row.get("sw_id"), idf_id)
            self.switch.addItem(str(label), row)
        if current_switch:
            wanted = (current_switch.get("sw_id") or "").casefold()
            for index in range(self.switch.count()):
                data = self.switch.itemData(index) or {}
                if (data.get("sw_id") or "").casefold() == wanted:
                    self.switch.setCurrentIndex(index)
                    break
        self.switch.blockSignals(False)
        self._refill_ports()
        self._refill_switch_ports()

    def _refill_ports(self, *_args) -> None:
        panel = self.panel.currentData()
        tables = dict(self.tables)
        tables["patches"] = self._all_patches()
        self.port.clear()
        if not panel:
            return
        for choice in open_panel_ports(
            panel,
            self.features,
            tables,
            self.media.currentData() or "copper",
            self.single.isChecked(),
        ):
            self.port.addItem(choice)

    def _refill_switch_ports(self, *_args) -> None:
        row = self.switch.currentData() or {}
        self.switch_port.clear()
        ports = open_switch_ports(row.get("sw_id") or "", self.tables.get("switch_ports") or [], self._all_patches())
        for port in ports:
            label = port.get("switch_port_name") or port.get("switch_port_id")
            self.switch_port.addItem(str(label), port)

    def _add(self) -> bool:
        panel = self.panel.currentData()
        switch = self.switch.currentData() or {}
        switch_port = self.switch_port.currentData() or {}
        if not self._idf_id() or not panel or not self.port.currentText() or not switch or not switch_port:
            QMessageBox.warning(self, "Patching", "Choose an IDF, panel, open port, switch, and switch port.")
            return False
        try:
            row = new_patch(
                {
                    "idf_id": self._idf_id(),
                    "media": self.media.currentData(),
                    "single_fiber": self.single.isChecked(),
                    "panel_id": feature_id(panel),
                    "panel_port": self.port.currentText(),
                    "sw_id": switch.get("sw_id"),
                    "switch_port_id": switch_port.get("switch_port_id"),
                    "switch_port_name": switch_port.get("switch_port_name"),
                }
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Patching", str(exc))
            return False
        self.pending.append(row)
        self._refill()
        return True

    def _accept(self) -> None:
        self.accept()
