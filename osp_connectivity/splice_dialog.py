"""Splice list for one enclosure, with a guided dialog for adding or editing a splice."""

import re
from copy import deepcopy

from qgis.PyQt.QtCore import QItemSelectionModel, Qt
from qgis.PyQt.QtGui import QColor, QFontDatabase, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QShortcut, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core.catalog import feature_id
from .core.enclosures import _range_text
from .core.notation import fiber_end, fiber_link
from .core.splice_editor import (
    available_fibers, cable_key, expand_groups, fiber_usage, resolve_cable, validate_groups,
)

SPLICE_TYPES = ('FUSION', 'EXPRESS', 'MECHANICAL')
STATUSES = ('proposed', 'installed', 'verified')
ERROR = '#b43535'
OK = '#187344'


def _range(first, last):
    if first is None:
        return '—'
    return str(first) if first == last or last is None else f'{first}-{last}'


def _runs(numbers):
    """Contiguous runs of fiber numbers as (first, last) pairs."""
    runs = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return [tuple(run) for run in runs]


def _is_saved(group):
    """True when a group is exactly what is stored in the project."""
    originals = group.get('_originals') or []
    if not originals:
        return False
    try:
        return expand_groups([group], originals[0].get('splice_enc_id') or '') == originals
    except (KeyError, TypeError, ValueError):
        return False


def _force_layers(cables, *identifiers):
    return any(sum(1 for cable in cables if str(feature_id(cable)).casefold() == str(identifier).casefold()) > 1
               for identifier in identifiers if identifier)


class SpliceEditorDialog(QDialog):
    COLUMNS = ('Cable A', 'Fibers A', 'Cable B', 'Fibers B', 'Type', 'Status', 'Tray', 'Loss dB', 'Notes', 'State')

    def __init__(self, model, parent=None, show_saved_route=None, print_saved_route=None):
        super().__init__(parent)
        self.model = deepcopy(model)
        self.groups = [deepcopy(group) for group in self.model['groups']]
        self._show_saved_route = show_saved_route
        self._print_saved_route = print_saved_route
        self._loading = True
        self._history = []
        self._position = -1
        self.setWindowTitle(f"Splice Editor — {feature_id(model['enclosure'])}")
        self.resize(1150, 780)
        layout = QVBoxLayout(self)
        intro = QLabel('Every splice and expressed fiber in this enclosure is listed below. Use <b>Add splice</b> to '
                       'connect fibers step by step, or select a splice and choose <b>Show saved route</b> to trace it. '
                       'Nothing is written to the project until you save.')
        intro.setWordWrap(True)
        layout.addWidget(intro)

        cables_box = QGroupBox('Cables in this enclosure')
        cables_layout = QVBoxLayout(cables_box)
        self.inventory = QTableWidget(len(self.model['cables']), 4)
        self.inventory.setHorizontalHeaderLabels(['Cable / layer', 'Fiber count', 'Used / available', 'Available fibers'])
        self.inventory.verticalHeader().hide()
        self.inventory.setMaximumHeight(150)
        self.inventory.setSelectionMode(QAbstractItemView.NoSelection)
        self.inventory.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.inventory.horizontalHeader().setStretchLastSection(True)
        for index, cable in enumerate(self.model['cables']):
            label = cable['label'] + (' (new at enclosure)' if cable.get('new') else '')
            self.inventory.setItem(index, 0, self._readonly(label))
            count = QSpinBox()
            count.setRange(0, max(10000, int(cable.get('capacity') or 0)))
            count.setSpecialValueText('Unknown — set count')
            count.setValue(int(cable.get('capacity') or 0))
            count.setEnabled(not cable.get('missing'))
            count.setToolTip('The number of fibers in this cable. It is saved to the cable with the splices.')
            count.valueChanged.connect(self._changed)
            self.inventory.setCellWidget(index, 1, count)
        cables_layout.addWidget(self.inventory)
        layout.addWidget(cables_box)

        splices_box = QGroupBox('Splices and expressed fibers')
        splices_layout = QVBoxLayout(splices_box)
        commands = QHBoxLayout()
        self.add_button = self._button('Add splice…', self._add, commands, 'Connect fibers step by step.')
        self.edit_button = self._button('Edit…', self._edit, commands, 'Change the selected splice. Double-click also edits.')
        self.delete_button = self._button('Delete', self._delete, commands, 'Remove the selected splices; their fibers become available.')
        self.express_button = self._button('Express spare fibers…', self._express, commands,
                                           'Pass every matching spare fiber straight through between two cables.')
        commands.addStretch()
        self.undo_button = self._button('Undo', self._undo, commands)
        self.redo_button = self._button('Redo', self._redo, commands)
        self._button('Copy table', self._copy, commands, 'Copy the splice list to the clipboard.')
        splices_layout.addLayout(commands)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('Filter by cable, fibers, type, tray, or notes…')
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        splices_layout.addWidget(self.filter)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_args: self._edit())
        splices_layout.addWidget(self.table)
        route_commands = QHBoxLayout()
        self.show_route = self._button('Show saved route', self._display_saved_route, route_commands,
                                       'Trace the selected splice through the saved project. Pending changes are excluded.')
        self.print_route = self._button('Print saved route PDF', self._print_route, route_commands,
                                        'Print a circuit card for the selected splice’s saved route.')
        route_commands.addStretch()
        splices_layout.addLayout(route_commands)
        self.saved_route = QPlainTextEdit()
        self.saved_route.setReadOnly(True)
        self.saved_route.setMaximumHeight(180)
        self.saved_route.hide()
        splices_layout.addWidget(self.saved_route)
        layout.addWidget(splices_box, 1)

        self.validation = QLabel()
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.undo_shortcut = QShortcut(QKeySequence.Undo, self, activated=self._undo)
        self.redo_shortcut = QShortcut(QKeySequence.Redo, self, activated=self._redo)
        self.delete_shortcut = QShortcut(QKeySequence.Delete, self.table, activated=self._delete)
        self._loading = False
        self._changed()

    # ----- helpers -------------------------------------------------------

    def _button(self, text, callback, layout, tip=None):
        button = QPushButton(text)
        button.setAutoDefault(False)
        if tip:
            button.setToolTip(tip)
        button.clicked.connect(lambda *_args: callback())
        layout.addWidget(button)
        return button

    @staticmethod
    def _readonly(text):
        item = QTableWidgetItem(str(text))
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _cable_text(self, identifier, layer):
        if not identifier:
            return '— open —'
        cable = resolve_cable(self.model['cables'], identifier, layer)
        return cable['label'] if cable else f'{identifier} — choose its cable layer'

    def _selected_rows(self):
        model = self.table.selectionModel()
        return sorted({index.row() for index in model.selectedRows()}) if model else []

    def selected_group(self):
        rows = self._selected_rows()
        return deepcopy(self.groups[rows[0]]) if len(rows) == 1 and rows[0] < len(self.groups) else None

    def _counts(self):
        return [self.inventory.cellWidget(index, 1).value() for index in range(self.inventory.rowCount())]

    def _snapshot(self):
        return deepcopy((self.groups, self._counts()))

    # ----- state ---------------------------------------------------------

    def _changed(self, *_args):
        if self._loading:
            return
        for cable, count in zip(self.model['cables'], self._counts()):
            cable['capacity'] = count
        state = self._snapshot()
        if self._position < 0 or state != self._history[self._position]:
            self._history = self._history[:self._position + 1] + [state]
            self._position += 1
        self._refresh()

    def _refresh(self, select=None):
        selected = self._selected_rows() if select is None else select
        problems = self._validate()
        bad_rows = {int(match) - 1 for problem in problems for match in re.findall(r'^Row (\d+):', problem)}
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.groups))
        for row, group in enumerate(self.groups):
            opened = str(group.get('splice_type') or '').upper() == 'OPEN'
            state = 'Saved' if _is_saved(group) else 'Edited' if group.get('_originals') else 'New'
            loss = group.get('loss_db')
            cells = (self._cable_text(group.get('a_cable_id'), group.get('a_layer_id')),
                     _range(group.get('a_from'), group.get('a_to')),
                     '— open —' if opened else self._cable_text(group.get('b_cable_id'), group.get('b_layer_id')),
                     '—' if opened else _range(group.get('b_from'), group.get('b_to')),
                     group.get('splice_type') or '', group.get('status') or '', group.get('tray') or '',
                     '' if loss in (None, '') else str(loss), group.get('notes') or '', state)
            for column, text in enumerate(cells):
                item = self._readonly(text)
                if row in bad_rows:
                    item.setForeground(QColor(ERROR))
                    item.setToolTip('\n'.join(problem for problem in problems if problem.startswith(f'Row {row + 1}:')))
                self.table.setItem(row, column, item)
        self.table.clearSelection()
        for row in selected:
            if 0 <= row < len(self.groups):
                self.table.selectionModel().select(self.table.model().index(row, 0),
                                                   QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.table.blockSignals(False)
        self._filter()
        self._selection_changed()
        self.undo_button.setEnabled(self._position > 0)
        self.redo_button.setEnabled(self._position + 1 < len(self._history))

    def _validate(self):
        """Refresh the cable availability and return every problem in the list."""
        problems = validate_groups(self.groups, self.model['cables'])
        used = fiber_usage(self.model['cables'], self.groups)
        for index, cable in enumerate(self.model['cables']):
            fibers = used.get(cable_key(cable), set())
            capacity = int(cable.get('capacity') or 0)
            self.inventory.setItem(index, 2, self._readonly(f'{len(fibers)} / {max(0, capacity - len(fibers))}'))
            spare = sorted(set(range(1, capacity + 1)) - fibers)
            text = _range_text(spare) or 'None' if capacity else 'Set the fiber count'
            self.inventory.setItem(index, 3, self._readonly(text))
        if problems:
            self.validation.setStyleSheet(f'color: {ERROR};')
            more = f'\n…and {len(problems) - 6} more.' if len(problems) > 6 else ''
            self.validation.setText('\n'.join(problems[:6]) + more)
        else:
            self.validation.setStyleSheet(f'color: {OK};')
            self.validation.setText(f'{len(self.groups)} splice range(s). All connections are valid. '
                                    'Changes apply only when you save.')
        return problems

    def _selection_changed(self):
        rows = self._selected_rows()
        single = len(rows) == 1
        self.edit_button.setEnabled(single)
        self.delete_button.setEnabled(bool(rows))
        self.show_route.setEnabled(single and self._show_saved_route is not None)
        self.print_route.setEnabled(single and self._print_saved_route is not None)

    def _filter(self, *_args):
        text = self.filter.text().strip().casefold()
        for row in range(self.table.rowCount()):
            cells = ' '.join(self.table.item(row, column).text() for column in range(self.table.columnCount())
                             if self.table.item(row, column))
            self.table.setRowHidden(row, bool(text and text not in cells.casefold()))

    # ----- commands ------------------------------------------------------

    def _wizard(self, group=None, others=None):
        wizard = SpliceWizard(self.model['cables'], self.groups if others is None else others, group, self)
        return wizard.result_group() if wizard.exec_() == QDialog.Accepted else None

    def _add(self):
        group = self._wizard()
        if group is not None:
            self.groups.append(group)
            self._changed()
            self._refresh(select=[len(self.groups) - 1])
            self.table.scrollToBottom()

    def _edit(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        row = rows[0]
        others = self.groups[:row] + self.groups[row + 1:]
        group = self._wizard(self.groups[row], others)
        if group is not None:
            self.groups[row] = group
            self._changed()
            self._refresh(select=[row])

    def _delete(self):
        rows = self._selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            if row < len(self.groups):
                self.groups.pop(row)
        self._changed()
        self._refresh(select=[])

    def _restore(self, state):
        self._loading = True
        groups, counts = deepcopy(state)
        self.groups = groups
        for index, count in enumerate(counts):
            self.inventory.cellWidget(index, 1).setValue(count)
        for cable, count in zip(self.model['cables'], counts):
            cable['capacity'] = count
        self._loading = False
        self._refresh(select=[])

    def _undo(self):
        if self._position > 0:
            self._position -= 1
            self._restore(self._history[self._position])

    def _redo(self):
        if self._position + 1 < len(self._history):
            self._position += 1
            self._restore(self._history[self._position])

    def _express(self):
        cables = [cable for cable in self.model['cables'] if not cable.get('missing') and cable.get('capacity')]
        if len(cables) < 2:
            QMessageBox.information(self, 'Express spare fibers',
                                    'Two cables with known fiber counts are needed to express fibers.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Express spare fibers')
        form = QFormLayout(dialog)
        form.addRow(QLabel('Every fiber number that is spare on both cables passes straight through.'))
        first, second = QComboBox(), QComboBox()
        for combo in (first, second):
            for cable in cables:
                combo.addItem(cable['label'], cable_key(cable))
        second.setCurrentIndex(1)
        form.addRow('From cable', first)
        form.addRow('To cable', second)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec_() != QDialog.Accepted:
            return
        a, b = cables[first.currentIndex()], cables[second.currentIndex()]
        if cable_key(a) == cable_key(b):
            QMessageBox.warning(self, 'Express spare fibers', 'Choose two different cables.')
            return
        spare = set(available_fibers(a, self.model['cables'], self.groups))
        spare &= set(available_fibers(b, self.model['cables'], self.groups))
        if not spare:
            QMessageBox.information(self, 'Express spare fibers', 'These cables have no matching spare fibers.')
            return
        force = _force_layers(self.model['cables'], feature_id(a), feature_id(b))
        for start, end in _runs(spare):
            self.groups.append({'a_cable_id': feature_id(a), 'a_layer_id': a.get('layer_id') or '',
                                'a_from': start, 'a_to': end,
                                'b_cable_id': feature_id(b), 'b_layer_id': b.get('layer_id') or '',
                                'b_from': start, 'b_to': end, 'splice_type': 'EXPRESS', 'status': 'proposed',
                                'tray': '', 'notes': '', 'loss_db': None, '_originals': [], '_force_layers': force})
        self._changed()

    def _copy(self):
        lines = ['\t'.join(('Connection', *self.COLUMNS))]
        for row, group in enumerate(self.groups):
            opened = str(group.get('splice_type') or '').upper() == 'OPEN'
            connection = fiber_link(group.get('a_cable_id'), group.get('a_from'), group.get('a_to'),
                                    None if opened else group.get('b_cable_id'),
                                    None if opened else group.get('b_from'), None if opened else group.get('b_to'))
            cells = [self.table.item(row, column).text() if self.table.item(row, column) else ''
                     for column in range(len(self.COLUMNS))]
            lines.append('\t'.join((connection, *cells)))
        QApplication.clipboard().setText('\n'.join(lines))

    def _call_route(self, callback, title):
        group = self.selected_group()
        if group is None or callback is None:
            return None
        try:
            return callback(group)
        except Exception as exc:  # A failed trace must never close the editor or lose pending changes.
            QMessageBox.warning(self, title, str(exc) or exc.__class__.__name__)
            return None

    def _display_saved_route(self):
        group = self.selected_group()
        text = self._call_route(self._show_saved_route, 'Show saved route')
        if text is None:
            return
        note = '' if _is_saved(group) else 'This splice has unsaved changes; the route below shows the saved project only.\n'
        title = fiber_end(group.get('a_cable_id'), group.get('a_from'), group.get('a_to'))
        self.saved_route.setPlainText(f'Saved route from {title} (pending editor changes excluded)\n{note}\n{text}')
        self.saved_route.show()

    def _print_route(self):
        self._call_route(self._print_saved_route, 'Print saved route PDF')

    def _save(self):
        problems = self._validate()
        if problems:
            QMessageBox.warning(self, 'Splice Editor', 'Fix these problems before saving:\n\n' + '\n'.join(problems[:12]))
            return
        self.accept()

    def choices(self):
        return deepcopy(self.groups)

    def reject(self):
        if self._history and self._snapshot() != self._history[0]:
            answer = QMessageBox.question(self, 'Discard splice changes?', 'Discard your unsaved splice changes?',
                                          QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Discard:
                return
        super().reject()


class SpliceWizard(QDialog):
    """Add or edit one splice: cable A, its fibers, cable B, its fibers, then the details."""

    STEPS = ('Choose cable A', 'Choose fibers on cable A', 'Choose cable B',
             'Choose fibers on cable B', 'Splice details')

    def __init__(self, cables, others, group=None, parent=None):
        super().__init__(parent)
        self.cables = cables
        self.others = [deepcopy(item) for item in others]
        self.original = deepcopy(group) if group else None
        self.setWindowTitle('Edit splice' if group else 'Add splice')
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setStyleSheet('font-weight: bold; font-size: 14px;')
        layout.addWidget(self.heading)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.back = QPushButton('< Back')
        self.next = QPushButton('Next >')
        self.cancel = QPushButton('Cancel')
        self.back.setAutoDefault(False)
        self.cancel.setAutoDefault(False)
        self.next.setDefault(True)
        self.back.clicked.connect(self._back)
        self.next.clicked.connect(self._next)
        self.cancel.clicked.connect(self.reject)
        for button in (self.back, self.next, self.cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.a_list = self._cable_page('Which cable does the splice start from?')
        self.a_from, self.a_to, self.a_free = self._range_page('a')
        self.b_list = self._cable_page('Which cable does it connect to?')
        self.b_from, self.b_to, self.b_free = self._range_page('b')
        self._details_page()

        self._fill_cables(self.a_list, None)
        if group:
            self._select_cable(self.a_list, group.get('a_cable_id'), group.get('a_layer_id'))
        self._loaded_a = None
        self._loaded_b = None
        self.a_list.currentRowChanged.connect(lambda *_args: self._update())
        self.b_list.currentRowChanged.connect(lambda *_args: self._update())
        for spin in (self.a_from, self.a_to, self.b_from):
            spin.valueChanged.connect(lambda *_args: self._update())
        self._go(0)

    # ----- pages ---------------------------------------------------------

    def _page(self, prompt):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(prompt)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.pages.addWidget(page)
        return layout

    def _cable_page(self, prompt):
        layout = self._page(prompt)
        cables = QListWidget()
        cables.itemDoubleClicked.connect(lambda *_args: self._next())
        layout.addWidget(cables)
        return cables

    def _range_page(self, side):
        layout = self._page('')
        prompt = layout.itemAt(0).widget()
        setattr(self, f'{side}_prompt', prompt)
        free = QLabel()
        free.setWordWrap(True)
        layout.addWidget(free)
        form = QFormLayout()
        first, last = QSpinBox(), QSpinBox()
        for spin in (first, last):
            spin.setRange(1, 1)
        row = QHBoxLayout()
        row.addWidget(first)
        row.addWidget(QLabel('to'))
        row.addWidget(last)
        row.addStretch()
        form.addRow('Fibers', row)
        layout.addLayout(form)
        if side == 'a':
            first.valueChanged.connect(lambda value: last.setValue(value) if last.value() < value else None)
            fill = QPushButton('Use the whole free run from here')
            fill.setAutoDefault(False)
            fill.clicked.connect(self._fill_run)
            layout.addWidget(fill)
        else:
            last.setEnabled(False)
            last.setToolTip('Cable B uses the same number of fibers as cable A.')
        layout.addStretch()
        return first, last, free

    def _details_page(self):
        layout = self._page('Describe the splice, then choose Finish.')
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        layout.addWidget(self.summary)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(SPLICE_TYPES)
        self.kind.setToolTip('EXPRESS keeps the same fiber numbers on both cables.')
        self.status = QComboBox()
        self.status.setEditable(True)
        self.status.addItems(STATUSES)
        self.tray = QLineEdit()
        self.loss = QLineEdit()
        self.loss.setPlaceholderText('e.g. 0.05')
        self.notes = QLineEdit()
        form.addRow('Splice type', self.kind)
        form.addRow('Status', self.status)
        form.addRow('Tray', self.tray)
        form.addRow('Loss dB', self.loss)
        form.addRow('Notes', self.notes)
        layout.addLayout(form)
        layout.addStretch()
        for signal in (self.kind.currentTextChanged, self.status.currentTextChanged,
                       self.tray.textChanged, self.loss.textChanged, self.notes.textChanged):
            signal.connect(lambda *_args: self._update())
        group = self.original or {}
        kind = str(group.get('splice_type') or 'FUSION').upper()
        if kind not in SPLICE_TYPES and kind != 'OPEN':
            self.kind.addItem(kind)
        self.kind.setCurrentText(kind if kind != 'OPEN' else 'FUSION')
        self.status.setCurrentText(str(group.get('status') or ('' if group.get('_originals') else 'proposed')))
        self.tray.setText(str(group.get('tray') or ''))
        loss = group.get('loss_db')
        self.loss.setText('' if loss in (None, '') else str(loss))
        self.notes.setText(str(group.get('notes') or ''))

    # ----- data ----------------------------------------------------------

    def _usage_text(self, cable):
        capacity = int(cable.get('capacity') or 0)
        if cable.get('missing'):
            return 'cable missing from the project'
        if not capacity:
            return 'fiber count unknown — set it in the cable list'
        free = available_fibers(cable, self.cables, self.others)
        return f'{len(free)} of {capacity} fibers available'

    def _fill_cables(self, widget, exclude):
        current = widget.currentItem().data(Qt.UserRole) if widget.currentItem() else None
        widget.blockSignals(True)
        widget.clear()
        if widget is self.b_list:
            item = QListWidgetItem('Leave open — no cable B (fibers end here)')
            item.setData(Qt.UserRole, 'OPEN')
            widget.addItem(item)
        for index, cable in enumerate(self.cables):
            if exclude is not None and cable_key(cable) == exclude:
                continue
            item = QListWidgetItem(f"{cable['label']}    ·    {self._usage_text(cable)}")
            item.setData(Qt.UserRole, index)
            if cable.get('missing') or not int(cable.get('capacity') or 0):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            widget.addItem(item)
        for row in range(widget.count()):
            if current is not None and widget.item(row).data(Qt.UserRole) == current:
                widget.setCurrentRow(row)
        widget.blockSignals(False)

    def _select_cable(self, widget, identifier, layer):
        if widget is self.b_list and not identifier:
            widget.setCurrentRow(0)
            return
        cable = resolve_cable(self.cables, identifier, layer)
        if cable is None:
            return
        index = next(i for i, item in enumerate(self.cables) if item is cable)
        for row in range(widget.count()):
            if widget.item(row).data(Qt.UserRole) == index:
                widget.item(row).setFlags(widget.item(row).flags() | Qt.ItemIsEnabled)
                widget.setCurrentRow(row)

    def _chosen(self, widget):
        item = widget.currentItem()
        if item is None or not item.flags() & Qt.ItemIsEnabled:
            return None
        value = item.data(Qt.UserRole)
        return 'OPEN' if value == 'OPEN' else self.cables[value]

    def _opened(self):
        return self._chosen(self.b_list) == 'OPEN'

    def _free(self, cable):
        return set(available_fibers(cable, self.cables, self.others))

    def _prepare_a(self):
        cable = self._chosen(self.a_list)
        if cable is None or self._loaded_a is cable:
            return
        self._loaded_a = cable
        group = self.original or {}
        if resolve_cable(self.cables, group.get('a_cable_id'), group.get('a_layer_id')) is cable and group.get('a_from'):
            first, last = int(group['a_from']), int(group.get('a_to') or group['a_from'])
        else:
            runs = _runs(self._free(cable))
            first = last = runs[0][0] if runs else 1
        # Keep an out-of-range stored splice visible so validation can name it instead of clamping it away.
        capacity = max(1, int(cable.get('capacity') or 0), last)
        for spin in (self.a_from, self.a_to):
            spin.blockSignals(True)
            spin.setRange(1, capacity)
        self.a_from.setValue(first)
        self.a_to.setValue(last)
        for spin in (self.a_from, self.a_to):
            spin.blockSignals(False)
        self.a_prompt.setText(f"Which fibers on <b>{feature_id(cable)}</b> are spliced? Choose one fiber or a range.")
        self.a_free.setText(f"Available on {feature_id(cable)}: {_range_text(sorted(self._free(cable))) or 'none'}")

    def _prepare_b(self):
        a = self._chosen(self.a_list)
        b = self._chosen(self.b_list)
        if a is None or b in (None, 'OPEN'):
            return
        length = self.a_to.value() - self.a_from.value()
        key = (id(b), self.a_from.value(), self.a_to.value())
        if self._loaded_b == key:
            return
        self._loaded_b = key
        capacity = int(b.get('capacity') or 0)
        group = self.original or {}
        free = self._free(b)
        stored = resolve_cable(self.cables, group.get('b_cable_id'), group.get('b_layer_id')) is b and group.get('b_from')
        self.b_from.blockSignals(True)
        self.b_from.setRange(1, max(1, capacity - length, int(group['b_from']) if stored else 1))
        self.b_to.setRange(1, max(1, capacity, self.b_from.maximum() + length))
        if stored:
            first = int(group['b_from'])
        elif set(range(self.a_from.value(), self.a_to.value() + 1)) <= free:
            first = self.a_from.value()  # Matching numbers are the usual choice and allow EXPRESS.
        else:
            first = next((start for start, end in _runs(free) if end - start >= length), self.a_from.value())
        self.b_from.setValue(first)
        self.b_from.blockSignals(False)
        self.b_prompt.setText(f"Which fibers on <b>{feature_id(b)}</b> do fibers "
                              f"{_range(self.a_from.value(), self.a_to.value())} of <b>{feature_id(a)}</b> connect to? "
                              f"Choose the first fiber; the range is {length + 1} fiber(s) long.")
        self.b_free.setText(f"Available on {feature_id(b)}: {_range_text(sorted(free)) or 'none'}")

    def _fill_run(self):
        cable = self._chosen(self.a_list)
        if cable is None:
            return
        free = self._free(cable)
        start = self.a_from.value()
        if start not in free:
            return
        end = start
        while end + 1 in free:
            end += 1
        self.a_to.setValue(end)

    def result_group(self):
        a = self._chosen(self.a_list)
        b = self._chosen(self.b_list)
        if a is None:
            return None
        opened = b == 'OPEN' or b is None
        group = deepcopy(self.original) if self.original else {'_originals': []}
        length = self.a_to.value() - self.a_from.value()
        loss = self.loss.text().strip()
        try:
            loss = float(loss) if loss else None
        except ValueError:
            pass  # Kept as text so validation names the problem.
        group.update({
            'a_cable_id': feature_id(a), 'a_layer_id': a.get('layer_id') or '',
            'a_from': self.a_from.value(), 'a_to': self.a_to.value(),
            'b_cable_id': '' if opened else feature_id(b), 'b_layer_id': '' if opened else b.get('layer_id') or '',
            'b_from': None if opened else self.b_from.value(),
            'b_to': None if opened else self.b_from.value() + length,
            'splice_type': 'OPEN' if opened else self.kind.currentText().strip().upper(),
            'status': self.status.currentText().strip(), 'tray': self.tray.text().strip(),
            'loss_db': loss, 'notes': self.notes.text().strip(),
            '_force_layers': _force_layers(self.cables, feature_id(a), None if opened else feature_id(b)),
        })
        return group

    def _problems(self, step):
        """Problems that block leaving ``step``."""
        a, b = self._chosen(self.a_list), self._chosen(self.b_list)
        if step == 0 or a is None:
            return [] if a else ['Choose the cable the splice starts from.']
        if step == 1:
            taken = sorted(set(range(self.a_from.value(), self.a_to.value() + 1)) - self._free(a))
            return [f"{feature_id(a)} fiber(s) {_range_text(taken)} are already spliced."] if taken else []
        if step in (2, 3) and b is None:
            return ['Choose cable B, or leave the fibers open.']
        if step == 3 and b != 'OPEN':
            last = self.b_from.value() + self.a_to.value() - self.a_from.value()
            if last > int(b.get('capacity') or 0):
                return [f"{feature_id(b)} only has {b.get('capacity')} fibers."]
            taken = sorted(set(range(self.b_from.value(), last + 1)) - self._free(b))
            return [f"{feature_id(b)} fiber(s) {_range_text(taken)} are already spliced."] if taken else []
        group = self.result_group()
        if group is None:
            return ['Choose cable A.']
        if group['loss_db'] is not None and not isinstance(group['loss_db'], float):
            return ['Loss must be a number, such as 0.05, or left blank.']
        if not group['splice_type']:
            return ['Choose a splice type.']
        row = len(self.others) + 1
        return [problem.split(': ', 1)[-1] for problem in validate_groups([*self.others, group], self.cables)
                if problem.startswith(f'Row {row}:')]

    # ----- navigation ----------------------------------------------------

    def _go(self, step):
        self.step = step
        if step == 1:
            self._prepare_a()
        if step == 2:
            self._fill_cables(self.b_list, cable_key(self._chosen(self.a_list)))
            if self.b_list.currentRow() < 0:
                group = self.original or {}
                if group:
                    self._select_cable(self.b_list, group.get('b_cable_id'), group.get('b_layer_id'))
                if self.b_list.currentRow() < 0:
                    enabled = [row for row in range(1, self.b_list.count())
                               if self.b_list.item(row).flags() & Qt.ItemIsEnabled]
                    self.b_list.setCurrentRow(enabled[0] if enabled else 0)
        if step == 3:
            self._prepare_b()
        if step == 4:
            self.kind.setEnabled(not self._opened())
        self.pages.setCurrentIndex(step)
        self.heading.setText(f'Step {step + 1} of {len(self.STEPS)} — {self.STEPS[step]}')
        self.back.setEnabled(step > 0)
        self.next.setText('Finish' if step == len(self.STEPS) - 1 else 'Next >')
        self._update()

    def _update(self):
        if not hasattr(self, 'step'):
            return
        if self.step == 4:
            group = self.result_group()
            if group:
                kind = group['splice_type'].lower()
                self.summary.setText(fiber_link(group['a_cable_id'], group['a_from'], group['a_to'],
                                                group['b_cable_id'] or None, group['b_from'], group['b_to'])
                                     + ('' if group['splice_type'] == 'OPEN' else f'  ({kind})'))
        if self.step == 3 and self._chosen(self.b_list) not in (None, 'OPEN'):
            self.b_to.setValue(self.b_from.value() + self.a_to.value() - self.a_from.value())
        problems = self._problems(self.step)
        self.message.setStyleSheet(f'color: {ERROR};' if problems else '')
        self.message.setText('\n'.join(problems[:4]))
        self.next.setEnabled(not problems)

    def _back(self):
        if self.step == 4 and self._opened():
            self._go(2)
        elif self.step > 0:
            self._go(self.step - 1)

    def _next(self):
        if self._problems(self.step):
            return
        if self.step == 0:
            self._loaded_b = None
        if self.step == 2 and self._opened():
            self._go(4)
        elif self.step < len(self.STEPS) - 1:
            self._go(self.step + 1)
        else:
            self.accept()
