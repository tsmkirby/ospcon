"""Customer-facing notation and complete route report regressions."""
import unittest
from copy import deepcopy

from osp_connectivity.core.cards import circuit_card_pages, _pdf
from osp_connectivity.core.export import report_record
from osp_connectivity.core.strands import compress_splices
from osp_connectivity.core.trace import trace_selection, format_report
from osp_connectivity.core.naming import scoped_asset_id, local_asset_id
from osp_connectivity.tests.test_core import feature


class NotationTests(unittest.TestCase):
    def test_link_key_survives_recorded_as_built_strands_and_default_circuit_creation(self):
        ap = feature('ap', 2, 'AP-01', {'cable_ref': 'F-1'})
        cable = feature('cable', 1, 'F-1', {'media': 'fiber', 'strand_count': 144})
        before = trace_selection([ap], [ap, cable], {})['reports'][0]
        circuit = dict(circuit_id='ckt.AP-01.F-1', device_id='AP-01', cable_id='F-1', strand_from=47, strand_to=48)
        after = trace_selection([ap], [ap, cable], {'circuits':[circuit]})['reports'][0]
        self.assertEqual(before['link_id'], after['link_id'])

    def test_fiber_export_includes_the_saved_switch_patch_without_the_idf_prefix(self):
        ap = feature('ap', 2, 'AP', {'cable_ref': 'F-1'})
        cable = feature('cable', 1, 'F-1', {'media': 'fiber', 'strand_count': 2})
        circuit = dict(device_id='AP', cable_id='F-1', strand_from=1, strand_to=2,
                       idf_id='IDF101', panel_id='IDF101-FD-01', panel_port='1-2')
        patch = dict(panel_id='IDF101-FD-01', panel_port='1-2', sw_id='IDF101-SWITCH01',
                     switch_port_id='Te1/0/1', mdf_idf_id='IDF101', media_type='fiber')
        report = trace_selection([ap], [ap, cable], {'circuits':[circuit], 'patches':[patch]})['reports'][0]
        record = report_record(report)
        self.assertEqual((record['Panel'], record['Switch'], record['Switch port']), ('FD-01', 'SWITCH01', 'Te1/0/1'))

    def test_scoping_is_idempotent_and_display_strips_only_the_owning_idf(self):
        self.assertEqual(scoped_asset_id('PP-01', 'IDF101'), 'IDF101-PP-01')
        self.assertEqual(scoped_asset_id('IDF101-PP-01', 'IDF101'), 'IDF101-PP-01')
        self.assertEqual(local_asset_id('IDF101-PP-01', 'IDF101'), 'PP-01')
        self.assertEqual(local_asset_id('IDF102-PP-01', 'IDF101'), 'IDF102-PP-01')

    def test_scoped_copper_ids_never_leak_into_presented_links(self):
        from osp_connectivity.core.notation import horizontal_link
        self.assertEqual(horizontal_link('IDF101', 'IDF101-SWITCH01', 'Gi1/0/12', 'IDF101-PP-01', '12',
                                         'CB-01', 'Cat6', ['DJ-01'], ['AP-01']),
                         'IDF101 SWITCH01 Gi1/0/12 -- PP-01:12 -- [CB-01 Cat6] -- DJ-01 -- AP-01')

    def test_ranges_never_merge_across_enclosures_or_cable_layers(self):
        rows = [dict(a_cable_id='A', a_strand_no=i, b_cable_id='B', b_strand_no=i,
                     splice_type='FUSION', splice_enc_id=enc, a_layer_id=layer)
                for i, enc, layer in [(1, 'EN-1', 'a'), (2, 'EN-2', 'a'), (3, 'EN-2', 'b')]]
        lines = compress_splices(rows)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], 'A:1 ↔︎ B:1 (fusion @ EN-1)')

    def test_copper_chain_includes_switch_jack_and_ap_in_both_end_directions(self):
        for reverse in (False, True):
            ends = ('IDF-01', 'AP-01') if reverse else ('AP-01', 'IDF-01')
            values = dict(cable_type='Cat6', a_end=ends[0], z_end=ends[1])
            side = 'a' if reverse else 'z'
            values.update({f'{side}_panel': 'PP-01', f'{side}_port': '12'})
            cable = feature('cable', 1, 'CB-01', values)
            ap = feature('ap', 2, 'AP-01', {'cable_ref': 'CB-01'})
            jack = feature('data_jack', 3, 'DJ-01', {'cable_ref': 'CB-01'})
            idf = feature('idf', 4, 'IDF-01')
            patches = [dict(panel_id='PP-01', panel_port='12', sw_id='SW-01',
                            switch_port_id='Gi1/0/12', mdf_idf_id='IDF-01')]
            report = trace_selection([ap], [cable, ap, jack, idf], {'patches': patches})['reports'][0]
            expected = 'IDF-01 SW-01 Gi1/0/12 -- PP-01:12 -- [CB-01 Cat6] -- DJ-01 -- AP-01'
            self.assertEqual(report['horizontal_link'], expected)
            self.assertEqual(report_record(report)['Horizontal link'], expected)
            self.assertIn(expected, format_report({'reports': [report]}))

    def test_missing_switch_is_explicit_and_optional_jack_is_omitted(self):
        cable = feature('cable', 1, 'CB-01', dict(cable_type='Cat7', a_end='AP-01',
                                                 z_end='IDF-01', z_panel='PP-01', z_port='12'))
        ap = feature('ap', 2, 'AP-01', {'cable_ref': 'CB-01'})
        report = trace_selection([ap], [cable, ap], {})['reports'][0]
        self.assertIn('[switch unknown]', report['horizontal_link'])
        self.assertTrue(report['horizontal_link'].endswith('[CB-01 Cat7] -- AP-01'))
        self.assertTrue(report['warnings'])

    def test_route_inventory_includes_other_strands_open_enclosures_and_handholes(self):
        cable = feature('cable', 1, 'A', {'media': 'fiber', 'strand_count': 2})
        other = feature('cable', 2, 'B', {'media': 'fiber', 'strand_count': 2})
        ap = feature('ap', 3, 'AP', {'cable_ref': 'A'})
        rows = [dict(a_cable_id='A', a_strand_no=1, b_cable_id='B', b_strand_no=1,
                     splice_type='FUSION', splice_enc_id='EN-1'),
                dict(a_cable_id='A', a_strand_no=2, b_cable_id='', b_strand_no=None,
                     splice_type='OPEN', splice_enc_id='EN-2')]
        tables = {'splices': rows, 'circuits': [dict(device_id='AP', cable_id='A', media_type='fiber', strand_from=1)],
                  'handholes': [dict(cable_id='B', handhole_id='HH-1')]}
        original = deepcopy(tables)
        report = trace_selection([ap], [ap, cable, other], tables)['reports'][0]
        self.assertEqual(tables, original)
        self.assertEqual(report['strand_lines'], ['A:1 ↔︎ B:1 (fusion @ EN-1)'])
        self.assertIn('A:2 ↔︎ OPEN (open @ EN-2)', report['splice_lines'])
        self.assertEqual(set(report['enclosures']), {'EN-1', 'EN-2'})
        self.assertEqual(report['handholes'], ['HH-1'])
        text = '\n'.join(block['text'] for page in circuit_card_pages([report]) for block in page['blocks'])
        for value in ('A:2 ↔︎ OPEN', 'EN-1', 'EN-2', 'HH-1'):
            self.assertIn(value, text)
        self.assertNotIn('A:2 ↔︎ OPEN', report_record(report)['Splices'])
        self.assertEqual(report_record(report)['Splices'], 'A:1 ↔︎ B:1 (fusion @ EN-1)')

    def test_enclosure_can_seed_a_read_only_saved_trace(self):
        enc = feature('splice_enc', 3, 'EN', geometry='Point', coords=[(0, 0)])
        cable = feature('cable', 1, 'A', {'media': 'fiber', 'a_end': 'EN'})
        report = trace_selection([enc], [enc, cable], {})['reports'][0]
        self.assertIn('EN', report['enclosures'])
        self.assertIn((enc['layer_id'], enc['fid']), report['highlights']['enclosures'])

    def test_pdf_uses_arrow_font_and_unicode_mapping(self):
        data = _pdf(circuit_card_pages([{'strand_lines': ['A:1-2 ↔︎ B:47-48 (fusion @ EN)']}]))
        self.assertIn(b'/BaseFont /Symbol', data)
        self.assertIn(b'<AB> Tj', data)
        self.assertIn(b'<2194FE0E>', data)
        self.assertNotIn(b'?', data)


if __name__ == '__main__':
    unittest.main()
