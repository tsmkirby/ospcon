"""Regression coverage for bidirectional fiber edits and lossless native-table writes."""
from copy import deepcopy
import unittest

from osp_connectivity.core.splice_editor import (
    editor_model, expand_groups, group_rows, splice_changes, strict_range, validate_groups,
)
from osp_connectivity.core.trace import _walk_strands
from osp_connectivity.tests.test_core import feature


def sample():
    enclosure = feature('splice_enc', 1, 'EN-1', coords=[(0, 0)], geometry='Point')
    cables = [feature('cable', i, name, {'media': 'fiber', 'strand_count': count},
                      coords=[(0, 0), (i * 10, i * 10)], layer_id='cables')
              for i, (name, count) in enumerate((('A', 144), ('B', 144), ('C', 12), ('D', 12)), 1)]
    rows = [dict(splice_id=f'sp.{number}', splice_enc_id='EN-1',
                 a_cable_id='A', a_strand_no=number, b_cable_id='B', b_strand_no=number,
                 splice_type='EXPRESS', status='installed', loss_db=0.03, spliced_by='Technician',
                 _fid=number, _layer_id='splices') for number in range(1, 145)]
    return enclosure, cables, rows


def connection(a='A', a_from=1, a_to=2, b='C', b_from=1, b_to=2, kind='FUSION'):
    return dict(a_cable_id=a, a_layer_id='cables', a_from=a_from, a_to=a_to,
                b_cable_id=b, b_layer_id='cables', b_from=b_from, b_to=b_to,
                splice_type=kind, status='proposed')


class SpliceEditorTests(unittest.TestCase):
    def test_native_rows_roundtrip_preserves_ids_nulls_and_extra_metadata(self):
        enclosure, cables, rows = sample()
        rows[0]['status'] = None
        rows[0]['b_layer_id'] = None
        model = editor_model(enclosure, cables, {'splices': rows})
        self.assertEqual(expand_groups(model['groups'], 'EN-1'), rows)
        self.assertEqual(splice_changes(model, model['groups'])['delete'], [])
        self.assertEqual(splice_changes(model, model['groups'])['splices'], [])

    def test_editing_one_pair_keeps_other_sources_and_unrelated_enclosures(self):
        enclosure, cables, rows = sample()
        rows = rows[:2] + [dict(rows[0], a_cable_id='C', b_cable_id='D', _fid=300, splice_id='other')]
        unrelated = dict(rows[0], splice_enc_id='EN-2', _fid=500)
        model = editor_model(enclosure, cables, {'splices': rows + [unrelated]})
        groups = deepcopy(model['groups'])
        groups[0]['status'] = 'verified'
        changes = splice_changes(model, groups)
        self.assertEqual(changes['delete'], [('splices', 1), ('splices', 2)])
        self.assertEqual(len(changes['splices']), 2)
        self.assertTrue(all(row['spliced_by'] == 'Technician' for row in changes['splices']))
        self.assertNotIn(('splices', 300), changes['delete'])
        self.assertNotIn(('splices', 500), changes['delete'])

    def test_47_48_fusion_keeps_other_142_fibers_expressed(self):
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows})
        groups = [connection('A', 1, 46, 'B', 1, 46, 'EXPRESS'),
                  connection('A', 47, 48, 'C', 1, 2),
                  connection('A', 49, 144, 'B', 49, 144, 'EXPRESS')]
        self.assertEqual(validate_groups(groups, model['cables']), [])
        expanded = splice_changes(model, groups)['rows']
        self.assertEqual(sum(row['splice_type'] == 'EXPRESS' for row in expanded), 142)
        self.assertEqual([(row['a_strand_no'], row['b_strand_no']) for row in expanded if row['b_cable_id'] == 'C'], [(47, 1), (48, 2)])

    def test_checks_both_ends_for_overlap_and_capacity(self):
        enclosure, cables, _ = sample()
        model = editor_model(enclosure, cables, {})
        problems = validate_groups([connection(), connection('B', 1, 2)], model['cables'])
        self.assertTrue(any('already used' in problem for problem in problems))
        problems = validate_groups([connection(b_from=12, b_to=13)], model['cables'])
        self.assertTrue(any('only has 12' in problem for problem in problems))
        self.assertTrue(validate_groups([connection(b_to=3)], model['cables']))
        self.assertTrue(validate_groups([connection(kind='EXPRESS', b_from=2, b_to=3)], model['cables']))

    def test_unknown_cables_and_ambiguous_ids_cannot_be_silently_selected(self):
        enclosure, cables, _ = sample()
        duplicate = deepcopy(cables[0]); duplicate['layer_id'] = 'duplicate'
        model = editor_model(enclosure, cables + [duplicate], {})
        group = connection(); group['a_layer_id'] = ''
        self.assertTrue(any('ambiguous' in error for error in validate_groups([group], model['cables'])))
        model['cables'][0]['capacity'] = 0
        self.assertTrue(any('set the fiber count' in error for error in validate_groups([connection()], model['cables'])))

    def test_stored_member_with_geometry_away_from_enclosure_is_listed(self):
        enclosure, cables, rows = sample()
        cables[0]['coords'] = [(10, 10), (20, 20)]
        model = editor_model(enclosure, cables, {'splices': rows})
        self.assertIn('A', [cable['id'] for cable in model['cables']])
        self.assertEqual(len(model['groups']), 1)

    def test_scoped_membership_does_not_attach_a_different_cable_with_the_same_id(self):
        enclosure, cables, _ = sample()
        duplicate = deepcopy(cables[0])
        duplicate['layer_id'] = 'other-layer'
        duplicate['coords'] = [(200, 200), (300, 300)]
        model = editor_model(enclosure, cables + [duplicate], {
            'memberships': [{'splice_enc_id': 'EN-1', 'cable_id': 'A', 'cable_layer_id': 'cables'}]})
        self.assertNotIn('other-layer', [cable.get('layer_id') for cable in model['cables']])

    def test_registered_cable_away_from_enclosure_does_not_have_a_blank_end_rewritten(self):
        from osp_connectivity.core.enclosures import enclosure_edits
        enclosure, cables, _ = sample()
        cables[0]['coords'] = [(200, 200), (300, 300)]
        result = enclosure_edits({'enclosure': enclosure, 'cables': [cables[0]]}, None, {}, 1)
        self.assertEqual(result['edits'], [])

    def test_delete_all_removes_all_sources_and_cancel_model_is_immutable(self):
        enclosure, cables, rows = sample()
        model = editor_model(enclosure, cables, {'splices': rows})
        before = deepcopy(model)
        result = splice_changes(model, [])
        self.assertEqual(len(result['delete']), 144)
        self.assertEqual(result['splices'], [])
        self.assertEqual(model, before)

    def test_explicit_resolution_of_duplicate_ids_is_stored_with_the_layer(self):
        enclosure, cables, rows = sample()
        duplicate = deepcopy(cables[0]); duplicate['layer_id'] = 'duplicate'
        model = editor_model(enclosure, cables + [duplicate], {'splices': rows[:1]})
        group = model['groups'][0]
        group['a_layer_id'] = 'cables'
        group['b_layer_id'] = 'cables'
        group['_force_layers'] = True
        changes = splice_changes(model, [group])
        self.assertEqual(changes['splices'][0]['a_layer_id'], 'cables')
        self.assertEqual(changes['delete'], [('splices', 1)])

    def test_notes_case_change_is_not_lost(self):
        enclosure, cables, rows = sample()
        rows[0]['notes'] = 'check'
        model = editor_model(enclosure, cables, {'splices': rows[:1]})
        model['groups'][0]['notes'] = 'CHECK'
        result = splice_changes(model, model['groups'])
        self.assertEqual(result['splices'][0]['notes'], 'CHECK')

    def test_strict_ranges_reject_typos_reverse_and_zero(self):
        self.assertEqual(strict_range(' 47 - 48 '), (47, 48))
        for text in ('junk 1-2', '0', '3-2', '1,3', '2.5', '-1', '1-200junk'):
            with self.assertRaises(ValueError):
                strict_range(text)

    def test_trace_follows_multiple_enclosures_from_either_end_or_the_middle(self):
        rows = [dict(a_cable_id=a, a_strand_no=1, b_cable_id=b, b_strand_no=1,
                     splice_enc_id=enc, splice_type='FUSION') for a, b, enc in [('A', 'B', 'EN-1'), ('B', 'C', 'EN-2'), ('C', 'D', 'EN-3')]]
        for start in ('A', 'B', 'C', 'D'):
            result = _walk_strands(start, [1], rows)
            self.assertEqual(len(result), 3)
            self.assertEqual({row['splice_enc_id'] for row in result}, {'EN-1', 'EN-2', 'EN-3'})

    def test_text_loss_and_strand_fields_do_not_break_the_editor(self):
        enclosure, cables, rows = sample()
        rows = rows[:2]
        rows[0]['loss_db'] = '0.03'
        rows[1]['a_strand_no'] = '2'
        rows[1]['b_strand_no'] = 'x'
        model = editor_model(enclosure, cables, {'splices': rows})
        problems = validate_groups(model['groups'], model['cables'])
        self.assertFalse(any('loss' in problem for problem in problems))
        self.assertTrue(any('invalid fiber range on cable B' in problem for problem in problems))

    def test_trace_fibers_follows_only_the_selected_range(self):
        from osp_connectivity.core.trace import trace_fibers
        enclosure, cables, rows = sample()
        rows = rows[:2] + [dict(rows[0], a_cable_id='B', a_strand_no=1, b_cable_id='C', b_strand_no=5,
                                splice_enc_id='EN-2', splice_type='FUSION')]
        tables = {'splices': rows}
        result = trace_fibers('A', 'cables', 1, 1, [enclosure, *cables], tables)
        self.assertEqual(result['warnings'], [])
        hops = result['reports'][0]['circuit_route']['segments']
        self.assertIn(hops, (['A', 'B', 'C'], ['C', 'B', 'A']))  # route order, from either end
        result = trace_fibers('A', 'cables', 2, 2, [enclosure, *cables], tables)
        self.assertIn(result['reports'][0]['circuit_route']['segments'], (['A', 'B'], ['B', 'A']))
        self.assertTrue(trace_fibers('missing', '', 1, 1, cables, tables)['warnings'])


if __name__ == '__main__':
    unittest.main()
