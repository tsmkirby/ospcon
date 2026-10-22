"""Table editor for all fiber connections at a selected enclosure."""

from copy import deepcopy

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QShortcut, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QPlainTextEdit,
)

from .core.catalog import feature_id
from .core.enclosures import _range_text
from .core.splice_editor import cable_key, fiber_usage, strict_range, validate_groups
from .core.notation import fiber_link


class SpliceEditorDialog(QDialog):
    COLUMNS = ("Cable A", "Fibers A", "Cable B", "Fibers B", "Type", "Status", "Tray", "Loss dB", "Notes")

    def __init__(self, model, parent=None, show_saved_route=None, print_saved_route=None):
        super().__init__(parent)
        self.model = deepcopy(model)
        self.setWindowTitle(f"Splice Editor — {feature_id(model['enclosure'])}")
        self.resize(1150, 760)
        self._loading = True
        self._history = []
        self._position = -1
        self._originals = []
        layout = QVBoxLayout(self)
        description = QLabel("Edit every connection in this enclosure. Use a single fiber or an equal-length range on both sides. "
                             "Deleting a row leaves those fibers unconnected. Save applies the whole table.")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.inventory = QTableWidget(len(self.model['cables']), 4)
        self.inventory.setHorizontalHeaderLabels(["Cable / layer", "Total fibers", "Reserved / available", "Available fibers"])
        self.inventory.setMaximumHeight(160)
        self.inventory.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.inventory.horizontalHeader().setStretchLastSection(True)
        for index, cable in enumerate(self.model['cables']):
            label = cable['label'] + (" (new at enclosure)" if cable.get('new') else "")
            self.inventory.setItem(index, 0, self._readonly(label))
            count = QSpinBox()
            count.setRange(0, max(10000, int(cable.get('capacity') or 0)))
            count.setSpecialValueText("Unknown — set count")
            count.setValue(int(cable.get('capacity') or 0))
            count.setEnabled(not cable.get('missing'))
            count.valueChanged.connect(self._changed)
            self.inventory.setCellWidget(index, 1, count)
        layout.addWidget(self.inventory)
        commands = QHBoxLayout()
        for text, callback in (("Add splice", self._add), ("Delete selected", self._delete),
                               ("Undo", self._undo), ("Redo", self._redo), ("Copy table", self._copy)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            commands.addWidget(button)
        layout.addLayout(commands)
        route_commands = QHBoxLayout()
        self.show_route = QPushButton("Show saved route")
        self.show_route.setEnabled(show_saved_route is not None)
        self.show_route.setToolTip("Show stored project connections; pending editor changes are excluded.")
        route_commands.addWidget(self.show_route)
        self.print_route = QPushButton("Print saved route PDF")
        self.print_route.setEnabled(print_saved_route is not None)
        route_commands.addWidget(self.print_route)
        layout.addLayout(route_commands)
        self.saved_route = QPlainTextEdit()
        self.saved_route.setReadOnly(True)
        self.saved_route.setMaximumHeight(180)
        self.saved_route.hide()
        layout.addWidget(self.saved_route)

        def display_saved_route():
            self.saved_route.setPlainText("Saved project route (pending editor changes excluded)\n\n" + show_saved_route())
            self.saved_route.show()

        if show_saved_route:
            self.show_route.clicked.connect(display_saved_route)
        if print_saved_route:
            self.print_route.clicked.connect(lambda: print_saved_route())
        express = QHBoxLayout()
        express.addWidget(QLabel("Express available fibers from"))
        self.express_a, self.express_b = self._cable_combo(), self._cable_combo()
        express.addWidget(self.express_a)
        express.addWidget(QLabel("to"))
        express.addWidget(self.express_b)
        if self.express_b.count() > 1:
            self.express_b.setCurrentIndex(1)
        fill = QPushButton("Express spare fibers")
        fill.clicked.connect(self._express)
        express.addWidget(fill)
        layout.addLayout(express)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by cable, tray, type, or notes…")
        self.filter.textChanged.connect(self._filter)
        layout.addWidget(self.filter)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._changed)
        layout.addWidget(self.table)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.undo_shortcut = QShortcut(QKeySequence.Undo, self, activated=self._undo)
        self.redo_shortcut = QShortcut(QKeySequence.Redo, self, activated=self._redo)
        for group in self.model['groups']:
            self._append(group)
        self._loading = False
        self._changed()

    @staticmethod
    def _readonly(text):
        item = QTableWidgetItem(str(text))
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _cable_combo(self, identifier=None, layer=None, optional=False):
        combo = QComboBox()
        if optional:
            combo.addItem("— open —", ("", ""))
        for cable in self.model['cables']:
            combo.addItem(cable['label'], (cable.get('layer_id') or '', feature_id(cable)))
        if identifier:
            matches = [index for index in range(combo.count())
                       if combo.itemData(index) and str(combo.itemData(index)[1]).casefold() == str(identifier).casefold()]
            exact = [index for index in matches if layer and combo.itemData(index)[0] == layer]
            if len(exact) == 1:
                combo.setCurrentIndex(exact[0])
            elif len(matches) == 1:
                combo.setCurrentIndex(matches[0])
            else:
                combo.insertItem(0, f"{identifier} — choose its cable layer", ('', identifier))
                combo.setCurrentIndex(0)
        combo.currentIndexChanged.connect(self._changed)
        return combo

    def _append(self, group):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._originals.append(deepcopy(group.get('_originals') or []))
        self.table.setCellWidget(row, 0, self._cable_combo(group.get('a_cable_id'), group.get('a_layer_id')))
        self.table.setCellWidget(row, 2, self._cable_combo(group.get('b_cable_id'), group.get('b_layer_id'), optional=True))
        for column, side in ((1, 'a'), (3, 'b')):
            start, end = group.get(f'{side}_from'), group.get(f'{side}_to')
            text = '' if start is None else str(start) if start == end else f'{start}-{end}'
            self.table.setItem(row, column, QTableWidgetItem(text))
        for column, key, defaults in ((4, 'splice_type', ['FUSION', 'EXPRESS', 'MECHANICAL', 'OPEN']),
                                      (5, 'status', ['proposed', 'installed', 'verified'])):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(defaults)
            combo.setCurrentText(group.get(key) or ('' if self._originals[-1] else defaults[0]))
            combo.currentTextChanged.connect(self._changed)
            self.table.setCellWidget(row, column, combo)
        for column, key in ((6, 'tray'), (7, 'loss_db'), (8, 'notes')):
            value = group.get(key)
            self.table.setItem(row, column, QTableWidgetItem('' if value is None else str(value)))

    def _snapshot(self):
        rows = []
        for row in range(self.table.rowCount()):
            cells = []
            for col in range(len(self.COLUMNS)):
                widget = self.table.cellWidget(row, col)
                cells.append(widget.currentData() if col in (0, 2) else widget.currentText() if widget else self.table.item(row, col).text())
            rows.append(cells)
        return deepcopy((rows, self._originals, [self.inventory.cellWidget(i, 1).value() for i in range(self.inventory.rowCount())]))

    def _changed(self, *_args):
        if self._loading:
            return
        state = self._snapshot()
        if self._position < 0 or state != self._history[self._position]:
            self._history = self._history[:self._position + 1] + [state]
            self._position += 1
        self._validate()
        self._filter()

    def _groups(self):
        groups = []
        for row, (cells, originals) in enumerate(zip(self._snapshot()[0], self._originals), 1):
            try:
                a_from, a_to = strict_range(cells[1])
                opened = cells[4].strip().upper() == 'OPEN'
                b_from, b_to = (None, None) if opened else strict_range(cells[3])
                a_layer, a_id = cells[0] or ('', '')
                b_layer, b_id = cells[2] or ('', '')
                if not opened and not b_id:
                    raise ValueError('Select cable B, or choose OPEN.')
                loss = float(cells[7]) if cells[7].strip() else None
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Row {row}: {exc}') from exc
            groups.append({'a_cable_id': a_id, 'a_layer_id': a_layer, 'a_from': a_from, 'a_to': a_to,
                           'b_cable_id': '' if opened else b_id, 'b_layer_id': '' if opened else b_layer,
                           'b_from': b_from, 'b_to': b_to, 'splice_type': cells[4].strip().upper(),
                           'status': cells[5].strip(), 'tray': cells[6], 'loss_db': loss, 'notes': cells[8],
                           '_originals': deepcopy(originals),
                           '_force_layers': any(sum(1 for cable in self.model['cables']
                                                    if str(feature_id(cable)).casefold() == str(identifier).casefold()) > 1
                                                for identifier in (a_id, b_id) if identifier)})
        return groups

    def _validate(self):
        for index, cable in enumerate(self.model['cables']):
            cable['capacity'] = self.inventory.cellWidget(index, 1).value()
        try:
            groups = self._groups()
            problems = validate_groups(groups, self.model['cables'])
            used = fiber_usage(self.model['cables'], groups)
            for index, cable in enumerate(self.model['cables']):
                fibers = used[cable_key(cable)]
                capacity = cable['capacity']
                self.inventory.setItem(index, 2, self._readonly(f'{len(fibers)} / {max(0, capacity - len(fibers))}'))
                spare = sorted(set(range(1, capacity + 1)) - fibers)
                self.inventory.setItem(index, 3, self._readonly(_range_text(spare) if capacity else 'Set fiber count'))
            if problems:
                raise ValueError('\n'.join(problems[:6]))
            self.validation.setStyleSheet('color: #187344;')
            self.validation.setText(f'{len(groups)} ranges. All connections valid. Changes apply only when you save.')
            return True
        except ValueError as exc:
            self.validation.setStyleSheet('color: #b43535;')
            self.validation.setText(str(exc))
            return False

    def _add(self):
        self._loading = True
        self._append({'a_from': 1, 'a_to': 1, 'b_from': 1, 'b_to': 1, 'splice_type': 'FUSION'})
        row = self.table.rowCount() - 1
        if self.table.cellWidget(row, 2).count() > 2:
            self.table.cellWidget(row, 2).setCurrentIndex(2)
        self._loading = False
        self._changed()
        self.table.scrollToBottom()

    def _delete(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        self._loading = True
        for row in rows:
            self.table.removeRow(row)
            self._originals.pop(row)
        self._loading = False
        self._changed()

    def _restore(self, state):
        self._loading = True
        rows, originals, counts = deepcopy(state)
        self.table.setRowCount(0)
        self._originals = []
        for cells, original in zip(rows, originals):
            self._append({'_originals': original})
            row = self.table.rowCount() - 1
            for column, value in enumerate(cells):
                widget = self.table.cellWidget(row, column)
                if column in (0, 2):
                    match = next((index for index in range(widget.count())
                                  if tuple(widget.itemData(index) or ()) == tuple(value or ())), -1)
                    widget.setCurrentIndex(match)
                elif widget:
                    widget.setCurrentText(value)
                else:
                    self.table.item(row, column).setText(value)
        for index, count in enumerate(counts):
            self.inventory.cellWidget(index, 1).setValue(count)
        self._loading = False
        self._validate()
        self._filter()

    def _undo(self):
        if self._position > 0:
            self._position -= 1
            self._restore(self._history[self._position])

    def _redo(self):
        if self._position + 1 < len(self._history):
            self._position += 1
            self._restore(self._history[self._position])

    def _express(self):
        if not self._validate():
            QMessageBox.warning(self, 'Express spare fibers', 'Resolve the validation errors first.')
            return
        a_key, b_key = self.express_a.currentData(), self.express_b.currentData()
        if not a_key or not b_key or a_key == b_key:
            QMessageBox.warning(self, 'Express spare fibers', 'Choose two different cables.')
            return
        cables = self.model['cables']
        a = next(c for c in cables if (c.get('layer_id') or '', feature_id(c)) == a_key)
        b = next(c for c in cables if (c.get('layer_id') or '', feature_id(c)) == b_key)
        used = fiber_usage(cables, self._groups())
        available = set(range(1, min(a['capacity'], b['capacity']) + 1)) - used[cable_key(a)] - used[cable_key(b)]
        if not available:
            QMessageBox.information(self, 'Express spare fibers', 'These cables have no matching spare fibers.')
            return
        self._loading = True
        for interval in _range_text(sorted(available)).split(', '):
            first, last = strict_range(interval)
            self._append({'a_layer_id': a_key[0], 'a_cable_id': a_key[1], 'a_from': first, 'a_to': last,
                          'b_layer_id': b_key[0], 'b_cable_id': b_key[1], 'b_from': first, 'b_to': last,
                          'splice_type': 'EXPRESS', 'status': 'proposed'})
        self._loading = False
        self._changed()

    def _filter(self, *_args):
        if self._loading:
            return
        text = self.filter.text().strip().casefold()
        for row, cells in enumerate(self._snapshot()[0]):
            self.table.setRowHidden(row, bool(text and text not in str(cells).casefold()))

    def _copy(self):
        try:
            groups = self._groups()
        except ValueError as exc:
            QMessageBox.warning(self, 'Copy splice table', str(exc))
            return
        lines = ['\t'.join(('Connection', *self.COLUMNS))]
        for row, group in zip(self._snapshot()[0], groups):
            connection = fiber_link(group['a_cable_id'], group['a_from'], group['a_to'],
                                    group['b_cable_id'], group['b_from'], group['b_to'])
            lines.append(connection + '\t' + '\t'.join(str(cell[1] or '') if isinstance(cell, (tuple, list)) else str(cell) for cell in row))
        QApplication.clipboard().setText('\n'.join(lines))

    def _save(self):
        self.table.clearFocus()
        if not self._validate():
            QMessageBox.warning(self, 'Splice Editor', self.validation.text())
            return
        self.accept()

    def choices(self):
        return self._groups()

    def reject(self):
        if self._position > 0 and self._snapshot() != self._history[0]:
            answer = QMessageBox.question(self, 'Discard splice changes?', 'Discard your unsaved splice changes?',
                                          QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Discard:
                return
        super().reject()
