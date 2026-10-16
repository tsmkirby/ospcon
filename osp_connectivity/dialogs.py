"""Dialogs for associating a selection and for editing a splice."""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .core.associate import port_in_use
from .core.catalog import feature_id
from .core.export import COLUMNS, item_label
from .core.fields import parse_count, parse_range
from .core.strands import build_splice_rows, compress_splices, validate_edits


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
        for name in proposal.panels:
            self.panel.addItem(name)
        form.addRow("Panel", self.panel)
        self.port = QLineEdit()
        form.addRow("Port", self.port)

        self.strands = QLineEdit()
        self.strands.setPlaceholderText("1-2")
        form.addRow("Fiber strands", self.strands)
        self.strands.setVisible(proposal.media == "fiber")
        layout.addLayout(form)

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
        panel = self.panel.currentText().strip()
        port = self.port.text().strip()
        if self.proposal.idfs and (not panel or not port):
            QMessageBox.warning(self, "Associate selection", "Enter the panel and the port.")
            return
        if panel and port and port_in_use(self.occupied, panel, port):
            QMessageBox.warning(self, "Associate selection", f"{panel} port {port} is already used.")
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
        return {
            "a_role": self.a_end.currentText(),
            "z_role": self.z_end.currentText(),
            "idf_id": self.idf.currentData() or self.idf.currentText(),
            "panel_id": self.panel.currentText().strip(),
            "port": self.port.text().strip(),
            "strands": self.strands.text().strip(),
        }


class SpliceDialog(QDialog):
    def __init__(self, strand_count: int, from_cable: str, targets: list[dict], parent=None):
        super().__init__(parent)
        self.from_cable = from_cable
        self.targets = targets
        self.setWindowTitle("Fiber enclosure")
        self.resize(560, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Strands are expressed through on the same numbers. "
                "Add a fusion row only for the strands that are spliced to another cable."
            )
        )
        form = QFormLayout()
        self.count = QSpinBox()
        self.count.setRange(1, 864)
        self.count.setValue(max(1, strand_count or 1))
        self.count.valueChanged.connect(self._preview)
        form.addRow("Strand count", self.count)
        self.opens = QLineEdit()
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
        self._preview()

    def _add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem("1"))
        self.table.setItem(row, 1, QTableWidgetItem("1"))
        combo = QComboBox()
        for target in self.targets:
            combo.addItem(target["id"], target)
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
        problems = validate_edits(self.count.value(), self._fusions(), self._open_ranges())
        if problems:
            QMessageBox.warning(self, "Fiber enclosure", "\n".join(problems))
            return
        self.accept()

    def result_edits(self) -> dict:
        return {
            "strand_count": self.count.value(),
            "fusions": self._fusions(),
            "opens": self._open_ranges(),
        }


class ExportDialog(QDialog):
    """Choose which traced circuits and columns go into a CSV or Excel file."""

    def __init__(self, reports, parent=None):
        super().__init__(parent)
        self.reports = list(reports)
        self.setWindowTitle("Export report")
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Check the circuits and columns to write. Column names stay the same every time, so later exports can be added to this file."))

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
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, name)
            self.fields.addItem(item)
        lists.addWidget(self._boxed("Items", self.items))
        lists.addWidget(self._boxed("Fields", self.fields))
        layout.addLayout(lists)

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Create a new file", "new")
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
        self.accept()
