"""Modify what is terminated on a patch panel: copper cable ports, or fiber strands on positions."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .core.catalog import feature_id
from .core.panels import _format_ports
from .core.terminations import next_free_port, termination_groups

ERROR = "#b43535"
OK = "#187344"


def _readonly(text):
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class CopperTerminationDialog(QDialog):
    """Change the port each copper cable lands on. Devices, circuits, and patches follow their cable."""

    def __init__(self, panel_label, rows, validate, parent=None):
        super().__init__(parent)
        self.rows = rows
        self.validate = validate
        self.result_value = None
        self.setWindowTitle(f"Modify terminations — {panel_label}")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        intro = QLabel("Type a new port for any cable, then save. Leave a row as it is to keep that cable where it is. "
                       "Devices and circuits on a cable move with it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = _table(["Cable", "End", "Current port", "New port", "Devices"])
        self.table.setRowCount(len(rows))
        self.editors = []
        for index, row in enumerate(rows):
            self.table.setItem(index, 0, _readonly(feature_id(row["cable"])))
            self.table.setItem(index, 1, _readonly(row["end"].upper()))
            self.table.setItem(index, 2, _readonly(row["ports"] or "none"))
            editor = QLineEdit(row["ports"])
            editor.setPlaceholderText("e.g. 12")
            editor.textChanged.connect(lambda *_args: self._check())
            self.table.setCellWidget(index, 3, editor)
            self.editors.append(editor)
            self.table.setItem(index, 4, _readonly(", ".join(feature_id(device) for device in row["devices"]) or "—"))
        layout.addWidget(self.table, 1)
        if not rows:
            layout.addWidget(QLabel("No cable names this panel on either end."))
        self.move_patches = QCheckBox("Move switch patches to the new port with their cable")
        self.move_patches.setChecked(True)
        self.move_patches.setToolTip("Keeps each circuit connected to the same switch port. "
                                     "Clear it to leave the patch cords on the old ports.")
        self.move_patches.toggled.connect(lambda *_args: self._check())
        layout.addWidget(self.move_patches)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._check()

    def new_ports(self):
        return [editor.text().strip() for editor in self.editors]

    def _check(self):
        try:
            result = self.validate(self.new_ports(), self.move_patches.isChecked())
        except ValueError as exc:
            self.message.setStyleSheet(f"color: {ERROR};")
            self.message.setText(str(exc))
            self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
            return None
        changed = result["changed"]
        self.message.setStyleSheet(f"color: {OK};" if changed else "")
        self.message.setText("\n".join(f"{cable}: port {old or 'none'} → {new}" for cable, old, new in changed)
                             or "No changes yet.")
        self.buttons.button(QDialogButtonBox.Save).setEnabled(bool(changed))
        return result

    def _save(self):
        result = self._check()
        if result and result["changed"]:
            self.result_value = result
            self.accept()


class FiberTerminationDialog(QDialog):
    """Terminate strands of one or more cables onto positions, and remove terminations."""

    def __init__(self, panel_label, existing, candidates, capacity, validate, blocked, parent=None):
        super().__init__(parent)
        self.existing = existing
        self.groups = termination_groups(existing)
        self.candidates = candidates
        self.capacity = capacity
        self.validate = validate
        self.blocked = blocked
        self.removed_groups = set()
        self.pending = []
        self.result_value = None
        self.setWindowTitle(f"Modify terminations — {panel_label}")
        self.resize(820, 640)
        layout = QVBoxLayout(self)

        current = QGroupBox("Strand terminations on this panel")
        current_layout = QVBoxLayout(current)
        self.current = _table(["Cable", "Strands", "Ports", "Status", ""])
        self.current.setSelectionMode(QAbstractItemView.ExtendedSelection)
        current_layout.addWidget(self.current)
        row = QHBoxLayout()
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda *_args: self._mark(True))
        keep = QPushButton("Keep selected")
        keep.clicked.connect(lambda *_args: self._mark(False))
        for button in (remove, keep):
            button.setAutoDefault(False)
            row.addWidget(button)
        row.addStretch()
        current_layout.addLayout(row)
        layout.addWidget(current, 1)

        add = QGroupBox("Terminate strands")
        form = QFormLayout(add)
        self.cable = QComboBox()
        for candidate in candidates:
            where = "lands on this panel" if candidate["landing"] else "ends at this IDF"
            count = f"{candidate['count']} strands" if candidate["count"] else "strand count unknown"
            self.cable.addItem(f"{feature_id(candidate['cable'])} — {count}, {where}")
        self.cable.currentIndexChanged.connect(lambda *_args: self._cable_changed())
        form.addRow("Cable", self.cable)
        strands = QHBoxLayout()
        self.strand_from, self.strand_to = QSpinBox(), QSpinBox()
        for spin in (self.strand_from, self.strand_to):
            spin.setRange(1, 9999)
            spin.valueChanged.connect(lambda *_args: self._suggest())
        self.strand_from.valueChanged.connect(
            lambda value: self.strand_to.setValue(value) if self.strand_to.value() < value else None)
        strands.addWidget(self.strand_from)
        strands.addWidget(QLabel("to"))
        strands.addWidget(self.strand_to)
        strands.addStretch()
        form.addRow("Strands", strands)
        ports = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1, capacity or 9999)
        self.port.valueChanged.connect(lambda *_args: self._update_ports())
        self.port_range = QLabel()
        ports.addWidget(self.port)
        ports.addWidget(self.port_range)
        ports.addStretch()
        form.addRow("First port", ports)
        self.status = QComboBox()
        self.status.setEditable(True)
        self.status.addItems(["proposed", "installed", "verified"])
        form.addRow("Status", self.status)
        self.free = QLabel()
        self.free.setWordWrap(True)
        form.addRow("Free ports", self.free)
        self.add_button = QPushButton("Add to list")
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda *_args: self._add())
        form.addRow("", self.add_button)
        layout.addWidget(add)
        if not candidates:
            self.add_button.setEnabled(False)
            self.free.setText("No fiber cable lands on this panel or ends at its IDF.")

        pending = QGroupBox("To add")
        pending_layout = QVBoxLayout(pending)
        self.pending_table = _table(["Cable", "Strands", "Ports", "Status"])
        pending_layout.addWidget(self.pending_table)
        drop = QPushButton("Take selected off the list")
        drop.setAutoDefault(False)
        drop.clicked.connect(lambda *_args: self._drop())
        pending_layout.addWidget(drop, 0, Qt.AlignLeft)
        layout.addWidget(pending, 1)

        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._refresh_current()
        self._cable_changed()
        self._check()

    # ----- state ---------------------------------------------------------

    def removed(self):
        return [row for index in sorted(self.removed_groups) for row in self.groups[index]["rows"]]

    def additions(self):
        return [dict(item) for item in self.pending]

    def _candidate(self):
        index = self.cable.currentIndex()
        return self.candidates[index] if 0 <= index < len(self.candidates) else None

    def _pending_ports(self):
        return {item["port_from"] + offset for item in self.pending
                for offset in range(item["strand_to"] - item["strand_from"] + 1)}

    def _pending_strands(self, cable_id):
        return {strand for item in self.pending if feature_id(item["cable"]).casefold() == cable_id.casefold()
                for strand in range(item["strand_from"], item["strand_to"] + 1)}

    # ----- current terminations -----------------------------------------

    def _refresh_current(self):
        self.current.setRowCount(len(self.groups))
        for index, group in enumerate(self.groups):
            strands = _format_ports(range(group["strand_from"], group["strand_to"] + 1))
            ports = (_format_ports(range(group["port_from"], group["port_to"] + 1))
                     if group["port_from"] is not None else "unknown")
            cells = (group["cable_id"], strands, ports, group["status"],
                     "will be removed" if index in self.removed_groups else "")
            for column, text in enumerate(cells):
                item = _readonly(text)
                if index in self.removed_groups:
                    font = item.font()
                    font.setStrikeOut(column < 4)
                    item.setFont(font)
                self.current.setItem(index, column, item)

    def _mark(self, remove):
        rows = {index.row() for index in self.current.selectionModel().selectedRows()}
        if remove:
            self.removed_groups |= rows
        else:
            self.removed_groups -= rows
        self._refresh_current()
        self._update_ports()
        self._check()

    # ----- adding --------------------------------------------------------

    def _cable_changed(self):
        candidate = self._candidate()
        if candidate is None:
            return
        count = candidate["count"] or 9999
        taken = {row["strand"] for row in self.existing
                 if row["cable_id"].casefold() == feature_id(candidate["cable"]).casefold()}
        taken |= self._pending_strands(feature_id(candidate["cable"]))
        first = next((number for number in range(1, count + 1) if number not in taken), 1)
        for spin in (self.strand_from, self.strand_to):
            spin.blockSignals(True)
            spin.setRange(1, count)
        self.strand_from.setValue(first)
        last = first
        while last + 1 <= count and last + 1 not in taken:
            last += 1
        self.strand_to.setValue(last)
        for spin in (self.strand_from, self.strand_to):
            spin.blockSignals(False)
        self._suggest()

    def _blocked(self, candidate):
        return set(self.blocked(feature_id(candidate["cable"]), self.removed())) | self._pending_ports()

    def _suggest(self):
        candidate = self._candidate()
        if candidate is None:
            return
        length = self.strand_to.value() - self.strand_from.value() + 1
        self.port.setValue(next_free_port(self._blocked(candidate), length))
        self._update_ports()

    def _update_ports(self):
        candidate = self._candidate()
        length = self.strand_to.value() - self.strand_from.value() + 1
        last = self.port.value() + length - 1
        self.port_range.setText(f"to {last}" if length > 1 else "")
        if candidate is None:
            return
        blocked = self._blocked(candidate)
        top = self.capacity or max([0, *blocked, last]) + 12
        free = [number for number in range(1, top + 1) if number not in blocked]
        self.free.setText((_format_ports(free) or "none") + ("" if self.capacity else " (panel size unknown)"))

    def _add(self):
        candidate = self._candidate()
        if candidate is None:
            return
        item = {"cable": candidate["cable"], "end": candidate["end"], "strand_from": self.strand_from.value(),
                "strand_to": self.strand_to.value(), "port_from": self.port.value(),
                "status": self.status.currentText().strip() or "proposed"}
        try:
            self.validate(self.additions() + [item], self.removed())
        except ValueError as exc:
            self.message.setStyleSheet(f"color: {ERROR};")
            self.message.setText(str(exc))
            return
        self.pending.append(item)
        self._refresh_pending()
        self._cable_changed()
        self._check()

    def _drop(self):
        rows = sorted({index.row() for index in self.pending_table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.pending.pop(row)
        self._refresh_pending()
        self._cable_changed()
        self._check()

    def _refresh_pending(self):
        self.pending_table.setRowCount(len(self.pending))
        for index, item in enumerate(self.pending):
            length = item["strand_to"] - item["strand_from"]
            cells = (feature_id(item["cable"]), _format_ports(range(item["strand_from"], item["strand_to"] + 1)),
                     _format_ports(range(item["port_from"], item["port_from"] + length + 1)), item["status"])
            for column, text in enumerate(cells):
                self.pending_table.setItem(index, column, _readonly(text))

    # ----- saving --------------------------------------------------------

    def _check(self):
        changes = bool(self.pending or self.removed_groups)
        try:
            result = self.validate(self.additions(), self.removed())
        except ValueError as exc:
            self.message.setStyleSheet(f"color: {ERROR};")
            self.message.setText(str(exc))
            self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
            return None
        parts = []
        if self.pending:
            parts.append(f"{len(result['rows'])} strand(s) to terminate")
        if self.removed_groups:
            parts.append(f"{len(self.removed())} termination(s) to remove")
        self.message.setStyleSheet(f"color: {OK};" if changes else "")
        self.message.setText(", ".join(parts) + "." if parts else
                             "Choose a cable, its strands, and the first port, then Add to list.")
        self.buttons.button(QDialogButtonBox.Save).setEnabled(changes)
        return result

    def _save(self):
        result = self._check()
        if result is not None and (self.pending or self.removed_groups):
            self.result_value = {"additions": self.additions(), "removed": self.removed(), "result": result}
            self.accept()


class StrandListDialog(QDialog):
    """Every strand of one fiber cable and the circuit on it, as ``A end ↔︎ Z end-1``."""

    def __init__(self, listing, parent=None, show_saved_route=None, print_saved_route=None):
        super().__init__(parent)
        from qgis.PyQt.QtWidgets import QApplication, QPlainTextEdit

        self.listing = listing
        self._show_saved_route = show_saved_route
        self._print_saved_route = print_saved_route
        self.setWindowTitle(f"Strands — {listing['cable_id']}")
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        rows = listing["rows"]
        circuits = len({row["circuit"].rsplit("-", 1)[0] for row in rows if row["circuit"]})
        summary = QLabel(f"{listing['cable_id']}: {listing['count']} strands, {circuits} circuit(s). "
                         "A is the first termination on each strand's route (a device when there is one) "
                         "and Z the last; -1 and -2 are the fibers of a circuit.")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if not rows:
            layout.addWidget(QLabel("This cable has no strand count and no saved strands. Set its strand count first."))
        self.table = _table(["Strand", "Circuit", "Route"])
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.table.setItem(index, 0, _readonly(row["number"]))
            circuit = _readonly(row["circuit"] or "no circuit")
            if not row["circuit"]:
                circuit.setForeground(Qt.gray)
            self.table.setItem(index, 1, circuit)
            self.table.setItem(index, 2, _readonly(row["route"]))
        self.table.currentCellChanged.connect(lambda *_args: self._detail())
        layout.addWidget(self.table, 1)
        route = QHBoxLayout()
        self.show_route = QPushButton("Show saved route")
        self.show_route.setToolTip("Trace the selected strand's circuit through the saved project and highlight it.")
        self.show_route.clicked.connect(lambda *_args: self._route(self._show_saved_route, "Show saved route", True))
        self.print_route = QPushButton("Print saved route PDF")
        self.print_route.clicked.connect(lambda *_args: self._route(self._print_saved_route, "Print saved route PDF", False))
        for button, callback in ((self.show_route, show_saved_route), (self.print_route, print_saved_route)):
            button.setAutoDefault(False)
            button.setEnabled(callback is not None and bool(rows))
            route.addWidget(button)
        route.addStretch()
        layout.addLayout(route)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(150)
        layout.addWidget(self.detail, 1)
        for warning in listing.get("warnings") or []:
            note = QLabel(f"Warning: {warning}")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {ERROR};")
            layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = buttons.addButton("Copy list", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda *_args: QApplication.clipboard().setText(self.text()))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if rows:
            self.table.setCurrentCell(0, 1)

    def circuit_numbers(self, index=None):
        """The selected strand and every other strand of this cable in the same circuit."""
        index = self.table.currentRow() if index is None else index
        rows = self.listing["rows"]
        if not 0 <= index < len(rows):
            return []
        row = rows[index]
        if not row["circuit"]:
            return [row["number"]]
        circuit = (row["a_end"], row["z_end"])
        return [other["number"] for other in rows if other["circuit"] and (other["a_end"], other["z_end"]) == circuit]

    def _route(self, callback, title, display):
        from qgis.PyQt.QtWidgets import QMessageBox

        numbers = self.circuit_numbers()
        if callback is None or not numbers:
            return
        try:
            text = callback(numbers)
        except Exception as exc:  # A failed trace must not close the list.
            QMessageBox.warning(self, title, str(exc) or exc.__class__.__name__)
            return
        if display and text is not None:
            row = self.listing["rows"][self.table.currentRow()]
            heading = row["circuit"].rsplit("-", 1)[0] if row["circuit"] else f"strand {row['number']}"
            self.detail.setPlainText(f"Saved route for {heading} ({self.listing['cable_id']} strands "
                                     f"{_format_ports(numbers)})\n\n{text}")

    def text(self):
        lines = ["Strand\tCircuit\tRoute"]
        lines.extend(f"{row['number']}\t{row['circuit']}\t{row['route']}" for row in self.listing["rows"])
        return "\n".join(lines)

    def _detail(self):
        index = self.table.currentRow()
        if not 0 <= index < len(self.listing["rows"]):
            return
        row = self.listing["rows"][index]
        lines = [f"Strand {row['number']} of {self.listing['cable_id']}"]
        if row["circuit"]:
            size = "1-fiber" if row["size"] == 1 else f"{row['size']}-fiber"
            lines.append(f"Circuit: {row['circuit']}  ({size}, from {row['source']})")
        lines.append(f"Route: {row['route']}")
        lines.extend(row["splices"] or ["No splices on this strand."])
        self.detail.setPlainText("\n".join(lines))
