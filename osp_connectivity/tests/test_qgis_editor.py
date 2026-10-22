"""Optional offscreen PyQGIS checks. Run with the Python bundled with QGIS."""
import os
import tempfile
import unittest
from copy import deepcopy

try:
    from qgis.core import QgsApplication, QgsFeature, QgsField, QgsProject, QgsVectorLayer
    from qgis.PyQt.QtCore import QVariant
    from qgis.PyQt.QtWidgets import QMainWindow, QToolBar
    HAS_QGIS = True
except ImportError:
    HAS_QGIS = False


@unittest.skipUnless(HAS_QGIS, 'Requires the QGIS Python runtime')
class QgisEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        cls.app = QgsApplication.instance() or QgsApplication([], False)
        cls.app.initQgis()

    def test_earth_export_reports_empty_features_and_invalid_parts(self):
        from qgis.core import QgsGeometry
        from osp_connectivity.bridge import collect_earth_layers
        from osp_connectivity.core.earth import build_kml, export_note, empty_export_message
        project = QgsProject()
        layer = QgsVectorLayer('MultiLineString?crs=EPSG:4326', 'Cables', 'memory')
        project.addMapLayer(layer)
        try:
            for wkt in (
                'MULTILINESTRING ((-76 39, -76.1 39.1), (-76 39))',
                None,
            ):
                feature = QgsFeature(layer.fields())
                if wkt:
                    feature.setGeometry(QgsGeometry.fromWkt(wkt))
                self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
            collected = collect_earth_layers(project, [layer.id()], False)
            self.assertEqual(collected['skipped_geometry'], {'Cables': 1})
            _, summary = build_kml(collected['layers'])
            self.assertEqual(summary['features'], 1)
            self.assertEqual(summary['rejected'], 1)
            note = export_note(summary, collected)
            self.assertIn('Cables (1)', note)
            self.assertIn('Left out 1 feature part(s)', note)
            empty_fid = list(layer.getFeatures())[1].id()
            layer.selectByIds([empty_fid])
            collected = collect_earth_layers(project, [layer.id()], True)
            self.assertEqual(collected['layers'], [])
            self.assertIn('Cables (1)', empty_export_message(collected, True))
        finally:
            project.removeAllMapLayers()

    def test_new_panels_and_switches_have_idf_scoped_keys_and_local_labels(self):
        from osp_connectivity.dialogs import PatchPanelDialog, SwitchDialog, PatchDialog
        from osp_connectivity.tests.test_core import feature
        panel = PatchPanelDialog('IDF102', [], {'copper': 'PP-01'}, ['IDF101-PP-01'], [])
        panel._accept()
        self.assertEqual(panel.choices()[0]['panel_id'], 'IDF102-PP-01')
        switch = SwitchDialog('SWITCH01', ['IDF101-SWITCH01'], idf_id='IDF102')
        switch._accept()
        self.assertEqual(switch.choices()['sw_id'], 'IDF102-SWITCH01')
        self.assertEqual(switch.identifier.text(), 'SWITCH01')
        idf = feature('idf', 1, 'IDF102')
        pp = feature('patch_panel', 2, 'IDF102-PP-01', {'idf_ref': 'IDF102', 'port_count': 12})
        patches = PatchDialog([idf], [pp], {'switches': [dict(sw_id='IDF102-SWITCH01', mdf_idf_id='IDF102')]})
        self.assertEqual(patches.panel.currentText(), 'PP-01')
        self.assertEqual(patches.panel.currentData()['id'], 'IDF102-PP-01')
        self.assertEqual(patches.switch.currentText(), 'SWITCH01')
        self.assertEqual(patches.switch.currentData()['sw_id'], 'IDF102-SWITCH01')
        for dialog in (panel, switch, patches):
            dialog.deleteLater()

    def test_saved_route_traces_the_selected_splice_without_pending_edits(self):
        from unittest.mock import Mock, patch
        from qgis.PyQt.QtWidgets import QApplication
        from osp_connectivity.core.splice_editor import editor_model
        from osp_connectivity.splice_dialog import SpliceEditorDialog
        from osp_connectivity.tests.test_splice_editor import sample
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows})
        show, print_pdf = Mock(return_value='A:1-144 ↔︎ B:1-144 (express @ EN-1)'), Mock()
        dialog = SpliceEditorDialog(model, show_saved_route=show, print_saved_route=print_pdf)
        dialog._copy()
        self.assertIn('A:1-144 ↔︎ B:1-144', QApplication.clipboard().text())
        self.assertFalse(dialog.show_route.isEnabled())  # nothing selected yet
        dialog._delete()  # no selected rows; the saved model must remain untouched
        self.assertEqual(len(dialog.groups), 1)
        new = dict(a_cable_id='C', a_layer_id='cables', a_from=1, a_to=2, b_cable_id='D', b_layer_id='cables',
                   b_from=1, b_to=2, splice_type='FUSION', status='proposed', _originals=[])
        with patch.object(dialog, '_wizard', return_value=new):
            dialog._add()
        self.assertEqual(dialog.table.item(1, 9).text(), 'New')
        pending = dialog._snapshot()
        dialog.table.selectRow(0)
        self.assertTrue(dialog.show_route.isEnabled())
        dialog.show_route.click()
        dialog.print_route.click()
        self.assertEqual(show.call_args[0][0]['a_cable_id'], 'A')
        self.assertEqual((show.call_args[0][0]['a_from'], show.call_args[0][0]['a_to']), (1, 144))
        print_pdf.assert_called_once()
        self.assertEqual(dialog._snapshot(), pending)
        self.assertEqual(model['original'], rows)
        self.assertIn('pending editor changes excluded', dialog.saved_route.toPlainText())
        show.side_effect = RuntimeError('layer gone')
        with patch('osp_connectivity.splice_dialog.QMessageBox.warning') as warning:
            dialog.show_route.click()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)
        dialog.deleteLater()

    def test_dialog_undo_restores_rows_and_native_attributes(self):
        from unittest.mock import patch
        from osp_connectivity.core.splice_editor import editor_model, splice_changes
        from osp_connectivity.splice_dialog import SpliceEditorDialog
        from osp_connectivity.tests.test_splice_editor import sample
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows})
        dialog = SpliceEditorDialog(model)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 9).text(), 'Saved')
        self.assertEqual(splice_changes(dialog.model, dialog.choices())['splices'], [])
        new = dict(a_cable_id='C', a_layer_id='cables', a_from=1, a_to=1, b_cable_id='D', b_layer_id='cables',
                   b_from=1, b_to=1, splice_type='FUSION', status='proposed', _originals=[])
        with patch.object(dialog, '_wizard', return_value=new):
            dialog._add()
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog._undo()
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(splice_changes(dialog.model, dialog.choices())['splices'], [])
        dialog._redo()
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.table.selectRow(0)
        dialog._delete()
        self.assertEqual(dialog.table.rowCount(), 1)
        dialog._undo()
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.deleteLater()

    def test_wizard_walks_cable_a_fibers_cable_b_fibers_then_details(self):
        from osp_connectivity.core.splice_editor import editor_model, validate_groups
        from osp_connectivity.splice_dialog import SpliceWizard
        from osp_connectivity.tests.test_splice_editor import sample
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows[:46]})  # A/B 1-46 expressed
        wizard = SpliceWizard(model['cables'], model['groups'])
        self.assertFalse(wizard.next.isEnabled())  # no cable A yet
        wizard.a_list.setCurrentRow(0)  # A
        wizard._next()
        self.assertEqual(wizard.step, 1)
        self.assertEqual((wizard.a_from.value(), wizard.a_to.value()), (47, 47))  # first free fiber
        wizard.a_from.setValue(1)
        self.assertFalse(wizard.next.isEnabled())  # fiber 1 is already expressed
        wizard.a_from.setValue(47)
        wizard.a_to.setValue(48)
        wizard._next()
        self.assertEqual(wizard.b_list.item(0).text().split(' ')[0], 'Leave')
        labels = [wizard.b_list.item(row).text() for row in range(wizard.b_list.count())]
        self.assertFalse(any(label.startswith('A —') for label in labels))  # cable A is not offered again
        wizard.b_list.setCurrentRow(next(row for row, label in enumerate(labels) if label.startswith('C —')))
        wizard._next()
        self.assertEqual(wizard.step, 3)
        self.assertEqual(wizard.b_from.value(), 1)  # C has only 12 fibers, so 47 cannot match
        self.assertEqual(wizard.b_to.value(), 2)
        wizard._next()
        self.assertEqual(wizard.step, 4)
        self.assertIn('A:47-48', wizard.summary.text())
        wizard.loss.setText('abc')
        self.assertFalse(wizard.next.isEnabled())
        wizard.loss.setText('0.05')
        wizard.kind.setCurrentText('EXPRESS')
        self.assertFalse(wizard.next.isEnabled())  # express must keep fiber numbers
        wizard.kind.setCurrentText('FUSION')
        wizard._next()
        self.assertEqual(wizard.result(), wizard.Accepted)
        group = wizard.result_group()
        self.assertEqual((group['a_cable_id'], group['a_from'], group['a_to']), ('A', 47, 48))
        self.assertEqual((group['b_cable_id'], group['b_from'], group['b_to']), ('C', 1, 2))
        self.assertEqual(group['loss_db'], 0.05)
        self.assertEqual(validate_groups([*model['groups'], group], model['cables']), [])
        wizard.deleteLater()

    def test_wizard_can_leave_fibers_open_and_edit_keeps_native_rows(self):
        from osp_connectivity.core.splice_editor import editor_model, splice_changes
        from osp_connectivity.splice_dialog import SpliceWizard
        from osp_connectivity.tests.test_splice_editor import sample
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows})
        wizard = SpliceWizard(model['cables'], [], model['groups'][0])
        self.assertEqual(wizard.a_list.currentItem().text().split(' ')[0], 'A')
        for _ in range(4):
            wizard._next()
        self.assertEqual(wizard.kind.currentText(), 'EXPRESS')
        wizard._next()
        self.assertEqual(splice_changes(model, [wizard.result_group()])['splices'], [])  # unchanged edit
        wizard.deleteLater()
        wizard = SpliceWizard(model['cables'], [])
        wizard.a_list.setCurrentRow(2)  # C
        wizard._next(); wizard._next()
        wizard.b_list.setCurrentRow(0)  # leave open
        wizard._next()
        self.assertEqual(wizard.step, 4)
        self.assertFalse(wizard.kind.isEnabled())
        wizard._back()
        self.assertEqual(wizard.step, 2)
        wizard._next(); wizard._next()
        group = wizard.result_group()
        self.assertEqual((group['splice_type'], group['b_cable_id'], group['b_from']), ('OPEN', '', None))
        wizard.deleteLater()

    def test_splice_replacement_and_added_fields_roll_back_together(self):
        from osp_connectivity.bridge import EditSession, apply_inserts, delete_features, ensure_splice_fields
        from osp_connectivity.core.link import RowInsert
        layer = QgsVectorLayer('None', 'splices', 'memory')
        fields = [QgsField(name, QVariant.Int if name.endswith('_no') else QVariant.String)
                  for name in ('splice_id', 'splice_enc_id', 'a_cable_id', 'a_strand_no',
                               'b_cable_id', 'b_strand_no', 'splice_type')]
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        row = dict(splice_id='old', splice_enc_id='EN-1', a_cable_id='A', a_strand_no=1,
                   b_cable_id='B', b_strand_no=1, splice_type='EXPRESS')
        feature = QgsFeature(layer.fields())
        for key, value in row.items():
            feature[key] = value
        self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        fid = next(layer.getFeatures()).id()
        session = EditSession()
        session.edit(layer)
        ensure_splice_fields(layer)
        delete_features(session, {layer.id(): layer}, [(layer.id(), fid)])
        apply_inserts(session, {'fiber_splice': layer}, [RowInsert('fiber_splice', dict(row, splice_id='new'), 'splice')])
        self.assertEqual(next(layer.getFeatures())['splice_id'], 'new')
        session.rollback()
        self.assertEqual(next(layer.getFeatures())['splice_id'], 'old')
        self.assertEqual(layer.fields().indexFromName('notes'), -1)
        layer.rollBack()

    def test_reopening_a_relation_table_does_not_overwrite_its_rows(self):
        from osp_connectivity.bridge import _write_layer
        project = QgsProject()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'links.gpkg')
            fields = [QgsField('value', QVariant.String)]
            _write_layer(project, path, 'kept', 'Kept', 'None', fields)
            layer = next(iter(project.mapLayers().values()))
            feature = QgsFeature(layer.fields())
            feature['value'] = 'preserve me'
            layer.startEditing()
            self.assertTrue(layer.addFeature(feature))
            self.assertTrue(layer.commitChanges())
            project.removeMapLayer(layer.id())
            _write_layer(project, path, 'kept', 'Kept', 'None', fields)
            layer = next(iter(project.mapLayers().values()))
            self.assertEqual(layer.featureCount(), 1)
            self.assertEqual(next(layer.getFeatures())['value'], 'preserve me')
            project.removeAllMapLayers()

    def test_editor_saves_all_sources_and_other_enclosures_stay_intact(self):
        from unittest.mock import patch
        from qgis.PyQt.QtWidgets import QDialog
        from osp_connectivity.bridge import read_tables, scan
        from osp_connectivity.plugin import OspConnectivityPlugin
        from osp_connectivity.core.splice_editor import editor_model
        from osp_connectivity.splice_dialog import SpliceEditorDialog
        from osp_connectivity.tests.test_splice_editor import sample
        project = QgsProject()
        enclosure, cables, source_rows = sample()
        splice_layer = QgsVectorLayer('None', 'Splices', 'memory')
        fields = [QgsField(name, QVariant.Int if name.endswith('_no') else QVariant.String)
                  for name in ('splice_id', 'splice_enc_id', 'a_cable_id', 'a_strand_no',
                               'b_cable_id', 'b_strand_no', 'splice_type', 'status')]
        splice_layer.dataProvider().addAttributes(fields)
        splice_layer.updateFields()
        for row in (source_rows[0], dict(source_rows[0], splice_id='unrelated', splice_enc_id='EN-2')):
            feature = QgsFeature(splice_layer.fields())
            for key, value in row.items():
                index = feature.fields().indexFromName(key)
                if index >= 0:
                    feature[key] = value
            splice_layer.dataProvider().addFeatures([feature])
        member_layer = QgsVectorLayer('None?field=splice_enc_id:string&field=cable_id:string&field=cable_layer_id:string', 'Members', 'memory')
        strand_layer = QgsVectorLayer('None?field=strand_id:string&field=cable_id:string&field=strand_no:integer&field=source_layer_id:string&field=status:string', 'Strands', 'memory')
        for layer in (splice_layer, member_layer, strand_layer):
            project.addMapLayer(layer)
        layers, infos = scan(project)
        class Canvas:
            def refresh(self):
                pass
        class Iface:
            def mainWindow(self):
                return None
            def mapCanvas(self):
                return Canvas()
        plugin = OspConnectivityPlugin(Iface())
        plugin._show = lambda text: None
        original = read_tables(layers, infos)
        dialog = SpliceEditorDialog(editor_model(enclosure, cables, original))
        dialog.groups[0]['status'] = 'verified'
        with patch('osp_connectivity.plugin.SpliceEditorDialog', return_value=dialog), patch.object(dialog, 'exec_', return_value=QDialog.Accepted):
            plugin._modify_enclosure(project, layers, infos, {'fiber_splice': splice_layer,
                'splice_enc_cable': member_layer, 'fiber_strand': strand_layer}, cables, enclosure)
        saved = read_tables(layers, infos)
        ours = [row for row in saved['splices'] if row['splice_enc_id'] == 'EN-1']
        other = [row for row in saved['splices'] if row['splice_enc_id'] == 'EN-2']
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]['status'], 'verified')
        self.assertEqual(other[0]['splice_id'], 'unrelated')
        c_rows = [row for row in saved['strands'] if row['cable_id'] == 'C']
        self.assertEqual(len(c_rows), 12)
        splice_layer.undoStack().undo()
        self.assertEqual([row['status'] for row in read_tables(layers, infos)['splices'] if row['splice_enc_id'] == 'EN-1'], ['installed'])
        for layer in (splice_layer, member_layer, strand_layer):
            layer.rollBack()
        project.removeAllMapLayers()
        dialog.deleteLater()

    def test_toolbar_buttons_have_square_icons_and_accessible_names(self):
        from osp_connectivity.plugin import OspConnectivityPlugin
        class Iface:
            def __init__(self):
                self.window = QMainWindow()
                self.menu = []
            def mainWindow(self):
                return self.window
            def addToolBar(self, title):
                toolbar = QToolBar(title, self.window)
                self.window.addToolBar(toolbar)
                return toolbar
            def addPluginToMenu(self, title, action):
                self.menu.append(action)
            def addDockWidget(self, area, dock):
                self.window.addDockWidget(area, dock)
        iface = Iface()
        plugin = OspConnectivityPlugin(iface)
        plugin.initGui()
        self.assertEqual(len(iface.menu), len(plugin.actions))
        for action in plugin.actions:
            self.assertFalse(action.icon().pixmap(24, 24).isNull(), action.text())
            button = plugin.toolbar.widgetForAction(action)
            self.assertEqual(button.width(), button.height())
            self.assertEqual(button.accessibleName(), action.text())
        iface.window.deleteLater()


if __name__ == '__main__':
    unittest.main()
