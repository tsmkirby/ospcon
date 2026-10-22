"""Show Strands List: every strand of a fiber cable with its circuit as A end ↔︎ Z end-n."""
import unittest

from osp_connectivity.core.strandlist import strand_list
from osp_connectivity.core.strands import build_splice_rows
from osp_connectivity.tests.test_core import feature

ENDS = {name: name for name in ('a_end', 'z_end', 'a_panel', 'a_port', 'z_panel', 'z_port')}


def cable(fid, name, coords, **values):
    return feature('cable', fid, name, dict({'media': 'fiber', 'strand_count': 6}, **values),
                   coords=coords, layer_id='cables', fields=ENDS)


def place(role, fid, name, xy, **values):
    return feature(role, fid, name, values, coords=[xy], layer_id=role, geometry='Point',
                   fields={'cable_ref': 'cable_id', 'idf_ref': 'idf_ref'})


class StrandListTests(unittest.TestCase):
    def setUp(self):
        # IDF-1/FD-01 --F-1 (6 strands)--> EN-1: strands 1-2 fused to D-1, which runs to AP-0004;
        # strands 3-4 express on F-2 to IDF-2/FD-02; strand 5 to F-3, a single fiber to IDF-3/FD-03; strand 6 open.
        self.cables = [
            cable(1, 'F-1', [(0, 0), (50, 0)], a_end='IDF-1', a_panel='FD-01', a_port='1-6', z_end='EN-1'),
            cable(2, 'D-1', [(50, 0), (50, 30)], strand_count=2, a_end='EN-1', z_end='AP-0004'),
            cable(3, 'F-2', [(50, 0), (90, 0)], a_end='EN-1', z_end='IDF-2', z_panel='FD-02', z_port='1-6'),
            cable(4, 'F-3', [(50, 0), (50, -30)], strand_count=1, a_end='EN-1', z_end='IDF-3', z_panel='FD-03', z_port='7'),
        ]
        self.places = [place('idf', 10, 'IDF-1', (0, 0)), place('idf', 11, 'IDF-2', (90, 0)), place('idf', 12, 'IDF-3', (50, -30)),
                       place('fdp', 20, 'FD-01', (0, 0), idf_ref='IDF-1'), place('fdp', 21, 'FD-02', (90, 0), idf_ref='IDF-2'),
                       place('fdp', 22, 'FD-03', (50, -30), idf_ref='IDF-3'),
                       place('ap', 30, 'AP-0004', (50, 30), cable_ref='D-1'),
                       place('splice_enc', 40, 'EN-1', (50, 0))]
        self.splices = build_splice_rows(6, 'F-1', 'F-2', 'EN-1', [
            {'a_from': 1, 'a_to': 2, 'b_cable_id': 'D-1', 'b_start': 1},
            {'a_from': 5, 'a_to': 5, 'b_cable_id': 'F-3', 'b_start': 1}], [{'a_from': 6, 'a_to': 6}])
        self.splices.append(dict(splice_enc_id='EN-1', a_cable_id='F-1', a_strand_no=6, b_cable_id=None,
                                 b_strand_no=None, splice_type='OPEN'))
        self.features = self.cables + self.places

    def listing(self, cable_index=0, tables=None):
        tables = tables or {'splices': self.splices}
        return strand_list(self.cables[cable_index], self.features, tables)

    def circuits(self, cable_index=0, tables=None):
        return [(row['number'], row['circuit']) for row in self.listing(cable_index, tables)['rows']]

    def test_a_and_z_are_the_first_and_last_terminations_with_a_device_as_a(self):
        self.assertEqual(self.circuits(), [
            (1, 'AP-0004 ↔︎ IDF-1 FD-01:1-2-1'),
            (2, 'AP-0004 ↔︎ IDF-1 FD-01:1-2-2'),
            (3, 'IDF-1 FD-01:3-4 ↔︎ IDF-2 FD-02:3-4-1'),
            (4, 'IDF-1 FD-01:3-4 ↔︎ IDF-2 FD-02:3-4-2'),
            (5, 'IDF-1 FD-01:5 ↔︎ IDF-3 FD-03:7-1'),
            (6, '')])

    def test_the_same_circuit_reads_the_same_from_another_segment(self):
        self.assertEqual(self.circuits(1), [(1, 'AP-0004 ↔︎ IDF-1 FD-01:1-2-1'), (2, 'AP-0004 ↔︎ IDF-1 FD-01:1-2-2')])

    def test_route_column_reads_a_to_z_like_the_circuit(self):
        rows = self.listing()['rows']
        self.assertEqual(rows[0]['route'], 'AP-0004 (D-1:1) ↔︎ IDF-1 FD-01:1 (F-1:1)')
        self.assertEqual(rows[2]['route'], 'IDF-1 FD-01:3 (F-1:3) ↔︎ IDF-2 FD-02:3 (F-2:3)')

    def test_open_strand_shows_where_it_goes_without_a_circuit(self):
        row = self.listing()['rows'][5]
        self.assertEqual(row['circuit'], '')
        self.assertIn('left OPEN @ EN-1', row['route'])

    def test_a_circuit_record_sets_the_circuit_and_its_fiber_order(self):
        tables = {'splices': self.splices, 'circuits': [
            {'circuit_id': 'C-9', 'media_type': 'fiber', 'device_id': 'CAM-12', 'idf_id': 'IDF-2',
             'panel_id': 'FD-02', 'panel_port': '3-4', 'cable_id': 'F-2', 'strand_from': 3, 'strand_to': 4}]}
        rows = self.listing(0, tables)['rows']
        self.assertEqual([rows[2]['circuit'], rows[3]['circuit']],
                         ['CAM-12 ↔︎ IDF-2 FD-02:3-4-1', 'CAM-12 ↔︎ IDF-2 FD-02:3-4-2'])
        self.assertEqual(rows[2]['source'], 'circuit record')

    def test_a_simplex_panel_makes_one_fiber_circuits(self):
        for item in self.places:
            if item['role'] == 'fdp':
                item['values']['topology'] = 'simplex'
        self.assertEqual(self.circuits()[2:4], [(3, 'IDF-1 FD-01:3 ↔︎ IDF-2 FD-02:3-1'),
                                                (4, 'IDF-1 FD-01:4 ↔︎ IDF-2 FD-02:4-1')])

    def test_panel_to_panel_reads_in_route_order_from_any_segment(self):
        from osp_connectivity.core.fiberpath import route_lines, fiber_routes
        lines = route_lines(fiber_routes('F-2', 'cables', [3, 4], self.features, {'splices': self.splices})['routes'])
        self.assertEqual((lines[0], lines[-1]), ('From: IDF-1 FD-01:3-4 (F-1:3-4)', 'To: IDF-2 FD-02:3-4 (F-2:3-4)'))
        self.assertEqual(self.circuits(2)[2:4], [(3, 'IDF-1 FD-01:3-4 ↔︎ IDF-2 FD-02:3-4-1'),
                                                 (4, 'IDF-1 FD-01:3-4 ↔︎ IDF-2 FD-02:3-4-2')])

    def test_unknown_strand_count_uses_the_highest_saved_strand(self):
        self.cables[0]['values'].pop('strand_count')
        self.assertEqual(self.listing()['count'], 6)


if __name__ == '__main__':
    unittest.main()
