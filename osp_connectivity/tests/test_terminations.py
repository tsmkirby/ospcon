"""Modify terminations on a patch panel: copper cable ports and fiber strand positions."""
import unittest

from osp_connectivity.core.panels import panel_ports
from osp_connectivity.core.terminations import (
    blocked_ports, copper_changes, copper_rows, fiber_additions, fiber_candidates, fiber_terminations,
    next_free_port, panel_media, strict_ports, termination_groups,
)
from osp_connectivity.tests.test_core import feature

CABLE_FIELDS = {name: name for name in ('a_end', 'z_end', 'a_panel', 'a_port', 'z_panel', 'z_port')}


def cable(fid, name, **values):
    return feature('cable', fid, name, values, coords=[(0, 0), (10, 0)], layer_id='cables', fields=CABLE_FIELDS)


def panel(name='PP-01', role='patch_panel', **values):
    return feature(role, 50, name, dict({'idf_ref': 'IDF-1'}, **values), layer_id='panels', geometry='Point',
                   fields={'idf_ref': 'idf_ref'})


class CopperTerminationTests(unittest.TestCase):
    def setUp(self):
        self.panel = panel(port_count=24)
        self.cables = [cable(1, 'CB-01', media='copper', a_end='AP-01', z_end='IDF-1', z_panel='PP-01', z_port='1'),
                       cable(2, 'CB-02', media='copper', a_end='AP-02', z_end='IDF-1', z_panel='PP-01', z_port='2')]
        self.ap = feature('ap', 3, 'AP-01', {'cable_ref': 'CB-01', 'panel': 'PP-01', 'port': '1'}, layer_id='aps',
                          geometry='Point', fields={'cable_ref': 'cable_id', 'panel': 'patch_panel_id', 'port': 'patch_panel_port'})
        self.features = [self.panel, *self.cables, self.ap]
        self.tables = {
            'patches': [{'panel_id': 'PP-01', 'panel_port': '1', 'sw_id': 'SW1', 'switch_port_id': 'gi1/0/1', '_fid': 7, '_layer_id': 'xc'}],
            'circuits': [{'cable_id': 'CB-01', 'panel_id': 'PP-01', 'panel_port': '1', '_fid': 8, '_layer_id': 'ckt'}],
            'terminations': [{'cable_id': 'CB-01', 'pp_id': 'PP-01', 'panel_port': '1', '_fid': 9, '_layer_id': 'ct'}],
        }

    def test_copper_panel_lists_every_landing_cable_with_its_devices(self):
        self.assertEqual(panel_media(self.panel, self.features), 'copper')
        rows = copper_rows(self.panel, self.features)
        self.assertEqual([(row['cable']['values']['id'], row['end'], row['ports']) for row in rows],
                         [('CB-01', 'z', '1'), ('CB-02', 'z', '2')])
        self.assertEqual([device['values']['id'] for device in rows[0]['devices']], ['AP-01'])

    def test_moving_a_cable_moves_its_device_circuit_termination_and_switch_patch(self):
        rows = copper_rows(self.panel, self.features)
        result = copper_changes(self.panel, rows, ['12', ''], self.features, self.tables)
        self.assertEqual(result['cable_edits'], [{'layer_id': 'cables', 'fid': 1, 'canonical': 'z_port', 'value': '12'}])
        self.assertEqual(result['device_edits'], [{'layer_id': 'aps', 'fid': 3, 'canonical': 'port', 'value': '12'}])
        updates = {(update['layer_id'], update['fid']): update['values'] for update in result['row_updates']}
        self.assertEqual(updates, {('ckt', 8): {'panel_port': '12'}, ('ct', 9): {'panel_port': '12'},
                                   ('xc', 7): {'panel_port': '12'}})
        kept = copper_changes(self.panel, rows, ['12', ''], self.features, self.tables, move_patches=False)
        self.assertNotIn(('xc', 7), {(update['layer_id'], update['fid']) for update in kept['row_updates']})

    def test_two_cables_can_swap_ports(self):
        rows = copper_rows(self.panel, self.features)
        result = copper_changes(self.panel, rows, ['2', '1'], self.features, self.tables)
        self.assertEqual([edit['value'] for edit in result['cable_edits']], ['2', '1'])

    def test_conflicts_and_bad_ports_are_refused(self):
        rows = copper_rows(self.panel, self.features)
        cases = {('2', ''): 'both', ('25', ''): '24 ports', ('x', ''): 'port number', ('3-2', ''): 'upward'}
        for ports, message in cases.items():
            with self.subTest(ports=ports):
                with self.assertRaises(ValueError) as caught:
                    copper_changes(self.panel, rows, list(ports), self.features, self.tables)
                self.assertIn(message, str(caught.exception))
        other = feature('ap', 4, 'AP-09', {'panel': 'PP-01', 'port': '5'}, layer_id='aps', geometry='Point')
        with self.assertRaises(ValueError) as caught:
            copper_changes(self.panel, rows, ['5', ''], self.features + [other], self.tables)
        self.assertIn('AP-09', str(caught.exception))

    def test_unchanged_rows_make_no_edits(self):
        rows = copper_rows(self.panel, self.features)
        result = copper_changes(self.panel, rows, ['1', '2'], self.features, self.tables)
        self.assertEqual((result['cable_edits'], result['device_edits'], result['row_updates']), ([], [], []))

    def test_strict_ports(self):
        self.assertEqual(strict_ports(' 1-3, 9 '), [1, 2, 3, 9])
        for bad in ('', '0', '1,1', 'a', '1-', '4-2'):
            with self.assertRaises(ValueError):
                strict_ports(bad)


class FiberTerminationTests(unittest.TestCase):
    def setUp(self):
        self.panel = panel('FD-01', role='fdp', position_count=24)
        self.trunk = cable(1, 'F-1', media='fiber', strand_count=12, a_end='IDF-1', a_panel='FD-01', z_end='EN-1')
        self.spur = cable(2, 'F-2', media='fiber', strand_count=6, a_end='EN-1', z_end='IDF-1')
        self.features = [self.panel, self.trunk, self.spur]
        self.tables = {'terminations': []}

    def add(self, cable, first, last, port, tables=None, removed=()):
        result = fiber_additions(self.panel, [{'cable': cable, 'strand_from': first, 'strand_to': last, 'port_from': port,
                                               'end': fiber_candidates(self.panel, self.features)[0]['end']
                                               if cable is self.trunk else 'z'}],
                                 self.features, tables or self.tables, removed)
        return result

    def saved(self, rows):
        return {'terminations': [dict(row, _fid=index, _layer_id='ft') for index, row in enumerate(rows, 1)]}

    def test_candidates_are_landing_cables_then_cables_ending_at_the_idf(self):
        self.assertEqual(panel_media(self.panel, self.features), 'fiber')
        found = fiber_candidates(self.panel, self.features)
        self.assertEqual([(item['cable']['values']['id'], item['end'], item['landing']) for item in found],
                         [('F-1', 'a', True), ('F-2', 'z', False)])

    def test_strands_of_two_cables_land_on_new_ports_and_replace_the_assumed_block(self):
        # F-1 lands with no port, so it is assumed to fill ports 1-12 until it has strand terminations.
        self.assertTrue(all(port['occupied'] for port in panel_ports(self.panel, self.features, self.tables)['ports'][:12]))
        first = self.add(self.trunk, 1, 4, 13)
        second = self.add(self.spur, 1, 2, 17)
        rows = first['rows'] + second['rows']
        self.assertEqual([(row['cable_id'], row['strand_no'], row['panel_position']) for row in rows],
                         [('F-1', 1, '13'), ('F-1', 2, '14'), ('F-1', 3, '15'), ('F-1', 4, '16'), ('F-2', 1, '17'), ('F-2', 2, '18')])
        self.assertEqual(second['cable_edits'], [{'layer_id': 'cables', 'fid': 2, 'canonical': 'z_panel', 'value': 'FD-01'}])
        ports = panel_ports(self.panel, self.features, self.saved(rows))['ports']
        self.assertFalse(ports[0]['occupied'])  # the assumed block is gone
        self.assertEqual((ports[12]['cable_id'], ports[12]['cable_span']), ('F-1', 'strand 1'))
        self.assertEqual(termination_groups(fiber_terminations(self.panel, self.saved(rows)))[0]['port_to'], 16)

    def test_a_cable_can_take_its_own_assumed_ports(self):
        self.assertEqual(len(self.add(self.trunk, 1, 12, 1)['rows']), 12)

    def test_used_ports_and_terminated_strands_are_refused(self):
        tables = self.saved(self.add(self.trunk, 1, 2, 13)['rows'])
        cases = [((self.trunk, 1, 1, 20), 'already terminated'), ((self.spur, 1, 1, 14), 'already has F-1:2'),
                 ((self.trunk, 3, 3, 14), 'already has F-1:2'),
                 ((self.spur, 6, 7, 20), 'only has 6'), ((self.spur, 1, 2, 24), '24 ports')]
        for args, message in cases:
            with self.subTest(args=args[1:]):
                with self.assertRaises(ValueError) as caught:
                    self.add(*args, tables=tables)
                self.assertIn(message, str(caught.exception))
        # Before F-1 has strand terminations, its assumed block 1-12 is taken.
        with self.assertRaises(ValueError) as caught:
            self.add(self.spur, 1, 1, 3)
        self.assertIn('used by F-1', str(caught.exception))

    def test_removing_a_termination_frees_its_port_and_strand(self):
        tables = self.saved(self.add(self.trunk, 1, 2, 13)['rows'])
        removed = tables['terminations'][:1]
        self.assertEqual(self.add(self.trunk, 1, 1, 13, tables, removed)['rows'][0]['panel_position'], '13')
        self.assertNotIn(13, blocked_ports(self.panel, 'F-2', self.features, tables, removed))
        self.assertIn(14, blocked_ports(self.panel, 'F-2', self.features, tables, removed))

    def test_next_free_port_skips_a_run_that_does_not_fit(self):
        self.assertEqual(next_free_port({1, 2, 5}, 2), 3)
        self.assertEqual(next_free_port({1, 2, 4}, 2), 5)


if __name__ == '__main__':
    unittest.main()
