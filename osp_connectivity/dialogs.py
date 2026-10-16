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
    QWidget,
)

from .core.associate import port_in_use
from .core.catalog import feature_id
from .core.handholes import handhole_label
from .core.export import COLUMNS, item_label
from .core.fields import parse_count, parse_range
from .core.place import name_taken
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
        handholes = []
        for row in range(self.handholes.count()):
            item = self.handholes.item(row)
            if item.checkState() == Qt.Checked:
                handholes.append(item.data(Qt.UserRole))
        return {
            "a_role": self.a_end.currentText(),
            "z_role": self.z_end.currentText(),
            "idf_id": self.idf.currentData() or self.idf.currentText(),
            "panel_id": self.panel.currentText().strip(),
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
            self.panel.addItem(name)
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
            self.panel.setCurrentText(association["panel"])
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
            self.panel.addItem(name)
        if current:
            self.panel.setCurrentText(current)
        elif self.association.get("panel"):
            self.panel.setCurrentText(self.association["panel"])
        self.panel.blockSignals(False)

    def _accept(self) -> None:
        panel = self.panel.currentText().strip()
        port = self.port.text().strip()
        if self.association.get("idfs") and (not panel or not port):
            QMessageBox.warning(self, "Modify associations", "Enter the panel and the port.")
            return
        if panel and port and port_in_use(self.occupied, panel, port):
            QMessageBox.warning(self, "Modify associations", f"{panel} port {port} is already used.")
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
            "panel_id": self.panel.currentText().strip(),
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


class PatchPanelDialog(QDialog):
    """Name the panel added on an IDF. Copper and fiber cables are named separately."""

    def __init__(self, idf_id: str, groups: list[dict], suggestions: dict, taken, occupied, parent=None):
        super().__init__(parent)
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
            panel = self.panel.text().strip()
            if not panel:
                QMessageBox.warning(self, "Add patch panel", "Enter a panel id.")
                return
            if name_taken(panel, self.taken):
                QMessageBox.warning(self, "Add patch panel", f"{panel} is already used. Choose a different name.")
                return
            self.accept()
            return
        for row, group in zip(self.rows, self.groups):
            panel = row["panel"].text().strip()
            port = row["port"].text().strip()
            label = "Fiber" if group.get("media") == "fiber" else "Copper"
            if not panel:
                QMessageBox.warning(self, "Add patch panel", f"Enter the {label.lower()} panel id.")
                return
            if name_taken(panel, self.taken) or name_taken(panel, names):
                QMessageBox.warning(self, "Add patch panel", f"{panel} is already used. Choose a different name.")
                return
            if not port:
                QMessageBox.warning(self, "Add patch panel", f"Enter the port for {panel}.")
                return
            if port_in_use(self.occupied, panel, port):
                QMessageBox.warning(self, "Add patch panel", f"{panel} port {port} is already used.")
                return
            if group.get("media") == "fiber" and not parse_range(row["strands"].text()):
                QMessageBox.warning(self, "Add patch panel", "Enter the fiber strand numbers, such as 1-2.")
                return
            names.append(panel)
        self.accept()

    def choices(self) -> list[dict]:
        if not self.groups:
            return [{"panel_id": self.panel.text().strip(), "media": self.media.currentData(), "associate": False}]
        chosen = []
        for row, group in zip(self.rows, self.groups):
            chosen.append(
                {
                    "panel_id": row["panel"].text().strip(),
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
            "panel_id": self.panel.currentText().strip(),
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
            self.panel.addItem(name)
        if current:
            self.panel.setCurrentText(current)
        elif preferred:
            self.panel.setCurrentText(preferred)
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
            panel = self.panel.currentText().strip()
            port = self.port.text().strip()
            if not panel or not port:
                QMessageBox.warning(self, self.windowTitle(), "Enter the panel and the port.")
                return
            if port_in_use(self.occupied, panel, port):
                QMessageBox.warning(self, self.windowTitle(), f"{panel} port {port} is already used.")
                return
            if self.media.currentData() == "fiber" and not parse_range(self.strands.text()):
                QMessageBox.warning(self, self.windowTitle(), "Enter the fiber strand numbers, such as 1-2.")
                return
        self.accept()
